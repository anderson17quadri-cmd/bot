"""
executor.py  -  Execucao de compras/vendas via Jupiter
=========================================================
Usa a Jupiter API (agregador de swaps em Solana) para trocar SOL <-> token.

Em modo DRY_RUN (config.DRY_RUN = True, o valor por defeito), as funcoes
so pedem uma COTACAO real a Jupiter (para os numeros serem realistas) mas
NUNCA assinam nem enviam nenhuma transacao. E 100% seguro correr assim.

Em modo real (DRY_RUN = False), assina a transacao devolvida pela Jupiter
com a wallet do bot (solders) e envia-a para a rede via RPC.

Fluxo Jupiter (2 passos):
    1. GET  /quote  -> devolve a melhor rota e os montantes estimados
    2. POST /swap   -> devolve uma transacao (base64) pronta a assinar
"""

import base64
import time

import requests
from solders.transaction import VersionedTransaction
from solders.commitment_config import CommitmentLevel

import config
import wallet
import posicoes
import carteira

JUPITER_QUOTE_URL = "https://public.jupiterapi.com/quote"
JUPITER_SWAP_URL = "https://public.jupiterapi.com/swap"


def _obter_cotacao(mint_entrada: str, mint_saida: str, quantidade_lamports: int) -> dict:
    """Pede uma cotacao a Jupiter. Funciona igual em dry-run ou real -
    e so uma consulta, nao mexe em dinheiro nenhum."""
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
    """Pede a transacao a Jupiter, assina-a com a wallet do bot, envia-a
    para a rede, e devolve a assinatura (hash) da transacao.

    So deve ser chamada quando config.DRY_RUN == False.
    """
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

    return resultado["result"]  # assinatura da transacao


def comprar_token(mint: str, simbolo: str, valor_usd: float, preco_sol_usd: float) -> dict:
    """Compra 'valor_usd' dolares do token 'mint', pagando em SOL.

    'preco_sol_usd' e o preco atual do SOL em USD, usado so para converter
    o valor_usd em lamports de SOL a pedir na cotacao.

    Devolve um dict com o resultado (sucesso, dry_run, detalhes).
    """
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
        if not carteira.registar_compra(
            simbolo, valor_usd, mint=mint,
            preco_unitario_usd=preco_compra_estimado,
            quantidade_tokens=quantidade_tokens_estimada,
        ):
            return {
                "sucesso": False, "dry_run": True,
                "mensagem": f"[SIMULADO] Saldo virtual insuficiente para comprar {simbolo}",
            }
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

    # Modo real - assina e envia de verdade
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
    """Vende 'percentagem' (0-100) da posicao aberta no token 'mint',
    trocando de volta para SOL.
    """
    todas = posicoes.listar_posicoes_abertas()
    posicao = todas.get(mint)
    if not posicao:
        raise ValueError(f"Nao ha posicao aberta para o mint {mint}.")

    quantidade_a_vender = posicao["quantidade_tokens"] * (percentagem / 100.0)

    cotacao = _obter_cotacao(mint, config.MINT_SOL, int(quantidade_a_vender))

    if config.DRY_RUN:
        # Estima o valor recebido em USD via cotacao token -> USDC (mais
        # direto do que converter atraves de SOL)
        cotacao_usdc = _obter_cotacao(mint, config.MINT_USDC, int(quantidade_a_vender))
        valor_recebido_usd = float(cotacao_usdc["outAmount"]) / 1_000_000
        valor_investido_proporcional = (
            posicao["valor_investido_usd"] * (percentagem / 100.0)
        )
        # Preco efetivo de venda por unidade minima (o que o mercado pagou
        # de facto, ja com slippage incluido)
        preco_venda_usd = (
            valor_recebido_usd / quantidade_a_vender if quantidade_a_vender else None
        )
        aplicado = carteira.registar_venda(
            posicao["simbolo"], valor_recebido_usd, valor_investido_proporcional,
            mint=mint,
            preco_compra_usd=posicao.get("preco_compra_usd"),
            preco_venda_usd=preco_venda_usd,
            quantidade_tokens=quantidade_a_vender,
            sniper_rapido=bool(posicao.get("sniper_rapido", False)),
        )
        if not aplicado:
            # Rejeitado pela verificacao de sanidade (cotacao absurda) -
            # o "dinheiro" simulado nunca entrou, por isso a posicao
            # NAO e fechada nem reduzida (fica intacta para tentar depois)
            return {
                "sucesso": False, "dry_run": True,
                "mensagem": (
                    f"[SIMULADO] Venda de {posicao['simbolo']} rejeitada: cotacao "
                    f"anormal (${valor_recebido_usd:,.2f}). Posicao mantida aberta."
                ),
            }
        resultado = {
            "sucesso": True, "dry_run": True,
            "mensagem": (
                f"[SIMULADO] Vendido {percentagem}% de {posicao['simbolo']} "
                f"por ${valor_recebido_usd:.2f} "
                f"(investido: ${valor_investido_proporcional:.2f})"
            ),
        }
    else:
        assinatura = _executar_swap_real(cotacao)
        resultado = {
            "sucesso": True, "dry_run": False, "assinatura": assinatura,
            "mensagem": f"Vendido {percentagem}% de {posicao['simbolo']} - tx {assinatura[:12]}...",
        }

    # Atualiza/fecha a posicao consoante a percentagem vendida (so chega
    # aqui se a venda foi mesmo aplicada - ver o "return" acima)
    if percentagem >= 100:
        posicoes.fechar_posicao(mint)
    else:
        nova_quantidade = posicao["quantidade_tokens"] - quantidade_a_vender
        posicoes.atualizar_posicao(mint, quantidade_tokens=nova_quantidade)

    return resultado


# --------------------------------------------------------------------------
# Teste rapido:  python executor.py
# Faz so uma cotacao (nunca compra nada), para confirmar que a ligacao a
# Jupiter esta a funcionar.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"DRY_RUN ativo: {config.DRY_RUN}")
    print("A pedir uma cotacao de teste (SOL -> USDC)...")
    cot = _obter_cotacao(config.MINT_SOL, config.MINT_USDC, 10_000_000)  # 0.01 SOL
    print("outAmount:", cot.get("outAmount"), "(em unidades minimas de USDC)")

