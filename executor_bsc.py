"""
executor_bsc.py  -  Execucao de trades na BSC via PancakeSwap V2
=================================================================
O equivalente ao executor.py (Solana/Jupiter), mas para a BSC. Usa o
router da PancakeSwap V2 e assina com 'eth-account' (leve), enviando a
transacao por chamada RPC direta (eth_sendRawTransaction) - sem o
web3.py completo.

Duas funcoes do router:
  - swapExactETHForTokens : comprar token pagando em BNB
  - swapExactTokensForETH : vender token de volta para BNB (precisa de
    approve primeiro - ver nota na venda)

Cotacao (read-only, real): getAmountsOut(amountIn, [WBNB, token]) diz
quantos tokens recebes - usado no dry-run para numeros realistas e no
real para calcular a protecao de slippage (amountOutMin).

SEGURANCA (igual ao pump.fun): o envio on-chain so acontece se
  config.DRY_RUN == False  E  config.BSC_PERMITIR_ENVIO_REAL == True.
Antes de enviar, estimamos o gas (eth_estimateGas) - se falhar, e sinal
de que a transacao reverteria (ex: honeypot), e abortamos. Mesmo assim,
o caminho real NAO foi validado com um swap verdadeiro em mainnet.

Nunca levanta excecao para fora: falhas viram {"sucesso": False, ...}.
"""

import time

import requests
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

import config

WBNB = to_checksum_address(config.BSC_WBNB)

# Seletores das funcoes (primeiros 4 bytes do keccak da assinatura)
_SEL_AMOUNTS_OUT = keccak(text="getAmountsOut(uint256,address[])")[:4]
_SEL_SWAP_ETH_FOR_TOKENS = keccak(
    text="swapExactETHForTokens(uint256,address[],address,uint256)")[:4]


def _rpc(metodo: str, params: list):
    """Chamada JSON-RPC a BSC. Levanta em erro (quem chama trata)."""
    r = requests.post(config.BSC_RPC_URL, json={
        "jsonrpc": "2.0", "id": 1, "method": metodo, "params": params,
    }, timeout=20)
    r.raise_for_status()
    resposta = r.json()
    if "error" in resposta:
        raise RuntimeError(f"RPC BSC erro em {metodo}: {resposta['error']}")
    return resposta["result"]


def _quanto_recebo(amount_in_wei: int, token: str) -> int:
    """getAmountsOut: quantos tokens dao por 'amount_in_wei' de BNB.
    Read-only (eth_call) - funciona igual em dry-run ou real."""
    dados = _SEL_AMOUNTS_OUT + encode(
        ["uint256", "address[]"], [amount_in_wei, [WBNB, to_checksum_address(token)]]
    )
    saida = _rpc("eth_call", [
        {"to": to_checksum_address(config.PANCAKE_ROUTER), "data": "0x" + dados.hex()},
        "latest",
    ])
    amounts = decode(["uint256[]"], bytes.fromhex(saida[2:]))[0]
    return amounts[-1]  # o ultimo e a quantidade do token de saida


def _preco_bnb_usd() -> float:
    """Preco do BNB em USD, via getAmountsOut BNB->USDT (1 USDT ~ 1 USD)."""
    recebido = _quanto_recebo(10**18, config.BSC_USDT)  # 1 BNB -> ? USDT
    return recebido / 1e18  # USDT tem 18 decimais na BSC


def comprar_token(mint: str, simbolo: str, valor_usd: float) -> dict:
    """Compra 'valor_usd' do token (contrato 0x...) pagando em BNB.

    Aplica o limite BSC_MAX_TRADE_USD. Em dry-run so simula (com cotacao
    real da PancakeSwap). Em real, constroi + estima gas + [talvez] envia.
    """
    teto = min(valor_usd, config.BSC_MAX_TRADE_USD)
    if teto <= 0:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": "Valor invalido."}

    try:
        preco_bnb = _preco_bnb_usd()
        if preco_bnb <= 0:
            raise RuntimeError("preco do BNB indisponivel")
        valor_bnb = teto / preco_bnb
        amount_in_wei = int(valor_bnb * 1e18)
        tokens_estimados = _quanto_recebo(amount_in_wei, mint)
    except Exception as e:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": f"[BSC] cotacao falhou: {e}"}

    if tokens_estimados <= 0:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": "[BSC] sem rota de troca para este token."}

    # Preco por token em USD (para o historico/posicoes). Os tokens BSC
    # tem tipicamente 18 decimais; usamos isso como convencao aqui.
    preco_unit_usd = teto / (tokens_estimados / 1e18) if tokens_estimados else 0

    # ---------------- DRY-RUN ----------------
    if config.DRY_RUN:
        import carteira
        import posicoes
        if not carteira.registar_compra(simbolo, teto, mint=mint,
                                        preco_unitario_usd=preco_unit_usd,
                                        quantidade_tokens=tokens_estimados,
                                        chain="bsc"):
            return {"sucesso": False, "dry_run": True,
                    "mensagem": f"[SIMULADO][BSC] Saldo virtual insuficiente para {simbolo}"}
        posicoes.abrir_posicao(mint=mint, simbolo=simbolo, valor_investido_usd=teto,
                               preco_compra_usd=preco_unit_usd,
                               quantidade_tokens=tokens_estimados, dry_run=True)
        posicoes.atualizar_posicao(mint, chain="bsc")
        return {"sucesso": True, "dry_run": True, "chain": "bsc",
                "quantidade_tokens": tokens_estimados,
                "mensagem": f"[SIMULADO][BSC] Comprado ${teto:.2f} de {simbolo} via PancakeSwap"}

    # ---------------- REAL ----------------
    return _comprar_real(mint, simbolo, teto, amount_in_wei, tokens_estimados, preco_unit_usd)


def _comprar_real(mint, simbolo, valor_usd, amount_in_wei, tokens_estimados, preco_unit_usd) -> dict:
    """Constroi swapExactETHForTokens, estima o gas e - se a trava
    permitir - assina e envia. Documentado como NAO validado com um swap
    real em mainnet."""
    try:
        import wallet_bsc
        conta = wallet_bsc.obter_conta()
        de = conta.address

        # amountOutMin com folga de slippage (protege de derrapagem)
        folga = 1 - (config.SLIPPAGE_BPS / 10_000)
        amount_out_min = int(tokens_estimados * folga)
        prazo = int(time.time()) + 120  # a transacao expira em 2 min

        # swapExactETHForTokens(amountOutMin, path[], to, deadline); BNB vai no 'value'
        dados = _SEL_SWAP_ETH_FOR_TOKENS + encode(
            ["uint256", "address[]", "address", "uint256"],
            [amount_out_min, [WBNB, to_checksum_address(mint)], to_checksum_address(de), prazo],
        )

        nonce = int(_rpc("eth_getTransactionCount", [de, "pending"]), 16)
        gas_price = int(_rpc("eth_gasPrice", []), 16)

        tx = {
            "to": to_checksum_address(config.PANCAKE_ROUTER),
            "value": amount_in_wei,
            "data": "0x" + dados.hex(),
            "nonce": nonce,
            "gasPrice": gas_price,
            "chainId": config.BSC_CHAIN_ID,
            "from": de,
        }

        # eth_estimateGas: se reverter aqui (ex: honeypot), abortamos ANTES
        # de gastar. E a nossa "simulacao" na BSC.
        try:
            gas = int(_rpc("eth_estimateGas", [tx]), 16)
        except Exception as e:
            return {"sucesso": False, "dry_run": False, "chain": "bsc",
                    "mensagem": f"[BSC] estimativa de gas falhou (transacao reverteria, nao enviado): {e}"}
        tx["gas"] = int(gas * 1.2)  # margem de 20%
        tx.pop("from", None)  # o campo 'from' nao entra na assinatura

        # Trava final: so envia se explicitamente permitido
        if not config.BSC_PERMITIR_ENVIO_REAL:
            return {"sucesso": False, "dry_run": False, "chain": "bsc",
                    "mensagem": ("[BSC] gas estimado OK, mas envio real bloqueado "
                                 "(BSC_PERMITIR_ENVIO_REAL=false). Nada foi gasto.")}

        assinada = conta.sign_transaction(tx)
        tx_hash = _rpc("eth_sendRawTransaction", ["0x" + assinada.raw_transaction.hex()])

        import posicoes
        posicoes.abrir_posicao(mint=mint, simbolo=simbolo, valor_investido_usd=valor_usd,
                               preco_compra_usd=preco_unit_usd,
                               quantidade_tokens=tokens_estimados, dry_run=False)
        posicoes.atualizar_posicao(mint, chain="bsc")
        return {"sucesso": True, "dry_run": False, "chain": "bsc", "assinatura": tx_hash,
                "mensagem": f"[BSC] Comprado ${valor_usd:.2f} de {simbolo} - tx {tx_hash[:12]}..."}

    except Exception as e:
        return {"sucesso": False, "dry_run": False, "chain": "bsc",
                "mensagem": f"[BSC] falha ao construir/enviar: {e}"}


# --------------------------------------------------------------------------
# Teste rapido:  python3 executor_bsc.py <contrato>
# Faz so uma cotacao real (nunca compra), para confirmar a ligacao.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    token = sys.argv[1] if len(sys.argv) > 1 else config.BSC_USDT
    print(f"Preco do BNB: ${_preco_bnb_usd():.2f}")
    recebido = _quanto_recebo(10**17, token)  # 0.1 BNB
    print(f"0.1 BNB -> {recebido/1e18:,.4f} unidades do token {token[:10]}...")
