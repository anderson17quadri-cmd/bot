"""
executor.py  -  Execucao de compras/vendas via Jupiter
=========================================================
Modo DRY_RUN: so pede cotacao real, nunca assina nem envia nada.
Modo real: assina com solders e envia via RPC.
"""

import base64

import requests
from solders.transaction import VersionedTransaction

import config
import wallet
import posicoes

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"


def _obter_cotacao(mint_entrada: str, mint_saida: str, quantidade_lamports: int) -> dict:
    params = {
        "inputMint": mint_entrada,
        "outputMint": mint_saida,
        "amount": quantidade_lamports,
        "slippageBps": config.SLIPPAGE_BPS,
    }
    resposta = requests.get(JUPITER_QUOTE_URL, params=params, timeout=15)
    resposta.raise_for_status()
    return resposta.json()


def _executar_swap_real(cotacao: dict) -> str:
    keypair = wallet.obter_keypair()

    payload = {
        "quoteResponse": cotacao,
        "userPublicKey": str(keypair.pubkey()),
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    resposta = requests.post(JUPITER_SWAP_URL, json=payload, timeout=20)
    resposta.raise_for_status()
    tx_base64 = resposta.json()["swapTransaction"]

    tx_bytes = base64.b64decode(tx_base64)
    tx = VersionedTransaction.from_bytes(tx_bytes)
    tx_assinada = VersionedTransaction(tx.message, [keypair])

    tx_assinada_bytes = bytes(tx_assinada)
    tx_assinada_base64 = base64.b64encode(tx_assinada_bytes).decode("utf-8")

    payload_envio = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            tx_assinada_base64,
            {"encoding": "base64", "skipPreflight": False, "maxRetries": 3},
        ],
    }
    resposta_envio = requests.post(config.SOLANA_RPC_URL, json=payload_envio, timeout=30)
    resposta_envio.raise_for_status()
    resultado = resposta_envio.json()

    if "error" in resultado:
        raise RuntimeError(f"RPC recusou a transacao: {resultado['error']}")

    return resultado["result"]


def comprar_token(mint: str, simbolo: str, valor_usd: float, preco_sol_usd: float) -> dict:
    if valor_usd > config.MAX_TRADE_USD:
        raise ValueError(
            f"Tentativa de compra (${valor_usd}) excede o limite "
            f"MAX_TRADE_USD (${config.MAX_TRADE_USD}). Operacao bloqueada."
        )

    quantidade_sol = valor_usd / preco_sol_usd
    quantidade_lamports = int(quantidade_sol * 1_000_000_000)

    cotacao = _obter_cotacao(config.MINT_SOL, mint, quantidade_lamports)
    quantidade_tokens_estimada = float(cotacao["outAmount"])
    preco_compra_estimado = valor_usd / quantidade_tokens_estimada if quantidade_tokens_estimada else 0

    if config.DRY_RUN:
        posicoes.abrir_posicao(
            mint=mint, simbolo=simbolo, valor_investido_usd=valor_usd,
            preco_compra_usd=preco_compra_estimado,
            quantidade_tokens=quantidade_tokens_estimada, dry_run=True,
        )
        return {
            "sucesso": True, "dry_run": True,
            "mensagem": f"[SIMULADO] Comprado ${valor_usd} de {simbolo}",
            "quantidade_tokens": quantidade_tokens_estimada,
        }

    assinatura = _executar_swap_real(cotacao)
    posicoes.abrir_posicao(
        mint=mint, simbolo=simbolo, valor_investido_usd=valor_usd,
        preco_compra_usd=preco_compra_estimado,
        quantidade_tokens=quantidade_tokens_estimada, dry_run=False,
    )
    return {
        "sucesso": True, "dry_run": False, "assinatura": assinatura,
        "mensagem": f"Comprado ${valor_usd} de {simbolo} - tx {assinatura[:12]}...",
        "quantidade_tokens": quantidade_tokens_estimada,
    }


def vender_token(mint: str, percentagem: float) -> dict:
    todas = posicoes.listar_posicoes_abertas()
    posicao = todas.get(mint)
    if not posicao:
        raise ValueError(f"Nao ha posicao aberta para o mint {mint}.")

    quantidade_a_vender = posicao["quantidade_tokens"] * (percentagem / 100.0)

    cotacao = _obter_cotacao(mint, config.MINT_SOL, int(quantidade_a_vender))

    if config.DRY_RUN:
        resultado = {
            "sucesso": True, "dry_run": True,
            "mensagem": f"[SIMULADO] Vendido {percentagem}% de {posicao['simbolo']}",
        }
    else:
        assinatura = _executar_swap_real(cotacao)
        resultado = {
            "sucesso": True, "dry_run": False, "assinatura": assinatura,
            "mensagem": f"Vendido {percentagem}% de {posicao['simbolo']} - tx {assinatura[:12]}...",
        }

    if percentagem >= 100:
        posicoes.fechar_posicao(mint)
    else:
        nova_quantidade = posicao["quantidade_tokens"] - quantidade_a_vender
        posicoes.atualizar_posicao(mint, quantidade_tokens=nova_quantidade)

    return resultado


if __name__ == "__main__":
    print(f"DRY_RUN ativo: {config.DRY_RUN}")
    print("A pedir uma cotacao de teste (SOL -> USDC)...")
    cot = _obter_cotacao(config.MINT_SOL, config.MINT_USDC, 10_000_000)
    print("outAmount:", cot.get("outAmount"))
