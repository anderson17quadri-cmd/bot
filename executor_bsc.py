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
# Para VENDER: trocar o token de volta por BNB (precisa de approve antes)
_SEL_SWAP_TOKENS_FOR_ETH = keccak(
    text="swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)")[:4]
_SEL_APPROVE = keccak(text="approve(address,uint256)")[:4]        # ERC-20
_SEL_ALLOWANCE = keccak(text="allowance(address,address)")[:4]    # ERC-20
_SEL_BALANCE_OF = keccak(text="balanceOf(address)")[:4]           # ERC-20
# Maximo uint256 (approve "infinito", padrao para nao repetir o approve)
_MAX_UINT = 2**256 - 1


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


def _quanto_recebo_ao_vender(amount_token: int, token: str) -> int:
    """getAmountsOut ao contrario: quantos wei de BNB dao por
    'amount_token' unidades do token (caminho token -> WBNB)."""
    dados = _SEL_AMOUNTS_OUT + encode(
        ["uint256", "address[]"], [amount_token, [to_checksum_address(token), WBNB]]
    )
    saida = _rpc("eth_call", [
        {"to": to_checksum_address(config.PANCAKE_ROUTER), "data": "0x" + dados.hex()},
        "latest",
    ])
    amounts = decode(["uint256[]"], bytes.fromhex(saida[2:]))[0]
    return amounts[-1]  # wei de BNB de saida


def _preco_bnb_usd() -> float:
    """Preco do BNB em USD, via getAmountsOut BNB->USDT (1 USDT ~ 1 USD)."""
    recebido = _quanto_recebo(10**18, config.BSC_USDT)  # 1 BNB -> ? USDT
    return recebido / 1e18  # USDT tem 18 decimais na BSC


def valor_atual_usd(mint: str, quantidade_tokens: float) -> float | None:
    """Quanto vale AGORA, em USD, 'quantidade_tokens' deste token (se
    vendido a mercado). Usado pelo stop-loss/take-profit e pela
    reavaliacao da watchlist. Devolve None se nao houver rota (liquidez
    morta) ou se a cotacao falhar - nunca levanta excecao."""
    try:
        bnb_wei = _quanto_recebo_ao_vender(int(quantidade_tokens), mint)
        if bnb_wei <= 0:
            return None
        return (bnb_wei / 1e18) * _preco_bnb_usd()
    except Exception:
        return None


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


def vender_token(mint: str, percentagem: float) -> dict:
    """Vende 'percentagem' (0-100) da posicao aberta no token BSC,
    trocando de volta para BNB via PancakeSwap.

    Dry-run: simula com cotacao real e regista a venda na carteira
    virtual. Real: faz approve (se preciso) + swapExactTokensForETH,
    trancado por BSC_PERMITIR_ENVIO_REAL. Nunca levanta excecao.
    """
    import posicoes
    pos = posicoes.listar_posicoes_abertas().get(mint)
    if not pos:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": "[BSC] sem posicao aberta neste token."}

    quantidade_total = pos.get("quantidade_tokens", 0)
    quantidade_a_vender = int(quantidade_total * (percentagem / 100.0))
    if quantidade_a_vender <= 0:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": "[BSC] quantidade a vender invalida."}

    # Valor que recebes agora (cotacao real, serve para dry-run e slippage)
    try:
        bnb_wei = _quanto_recebo_ao_vender(quantidade_a_vender, mint)
        valor_recebido_usd = (bnb_wei / 1e18) * _preco_bnb_usd()
    except Exception as e:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": f"[BSC] cotacao de venda falhou: {e}"}

    if bnb_wei <= 0:
        return {"sucesso": False, "dry_run": config.DRY_RUN, "mensagem": "[BSC] sem rota de venda (liquidez morta?)."}

    # Proporcao do investido correspondente a esta venda (para o lucro)
    investido_proporcional = pos.get("valor_investido_usd", 0) * (percentagem / 100.0)
    preco_venda_usd = valor_recebido_usd / (quantidade_a_vender / 1e18) if quantidade_a_vender else None

    # ---------------- DRY-RUN ----------------
    if config.DRY_RUN:
        import carteira
        carteira.registar_venda(
            pos["simbolo"], valor_recebido_usd, investido_proporcional, mint=mint,
            preco_compra_usd=pos.get("preco_compra_usd"), preco_venda_usd=preco_venda_usd,
            quantidade_tokens=quantidade_a_vender, chain="bsc",
        )
        _fechar_ou_reduzir(mint, pos, quantidade_a_vender, percentagem)
        return {"sucesso": True, "dry_run": True, "chain": "bsc",
                "mensagem": (f"[SIMULADO][BSC] Vendido {percentagem:.0f}% de {pos['simbolo']} "
                             f"por ${valor_recebido_usd:.2f}")}

    # ---------------- REAL ----------------
    return _vender_real(mint, pos, quantidade_a_vender, percentagem, bnb_wei,
                        valor_recebido_usd, investido_proporcional, preco_venda_usd)


def _fechar_ou_reduzir(mint, pos, quantidade_vendida, percentagem):
    """Fecha a posicao (venda de 100%) ou reduz a quantidade restante."""
    import posicoes
    if percentagem >= 100:
        posicoes.fechar_posicao(mint)
    else:
        nova = pos.get("quantidade_tokens", 0) - quantidade_vendida
        posicoes.atualizar_posicao(mint, quantidade_tokens=nova)


def _enviar_tx(conta, tx_base: dict) -> str:
    """Estima o gas, assina e envia uma transacao. Devolve o hash.
    Levanta se a estimativa falhar (a transacao reverteria)."""
    de = conta.address
    tx = dict(tx_base)
    tx["nonce"] = int(_rpc("eth_getTransactionCount", [de, "pending"]), 16)
    tx["gasPrice"] = int(_rpc("eth_gasPrice", []), 16)
    tx["chainId"] = config.BSC_CHAIN_ID
    gas = int(_rpc("eth_estimateGas", [{**tx, "from": de}]), 16)
    tx["gas"] = int(gas * 1.2)
    assinada = conta.sign_transaction(tx)
    return _rpc("eth_sendRawTransaction", ["0x" + assinada.raw_transaction.hex()])


def _vender_real(mint, pos, quantidade, percentagem, bnb_wei, valor_recebido_usd,
                 investido_proporcional, preco_venda_usd) -> dict:
    """Caminho REAL da venda: approve (se preciso) + swap. NAO validado
    com um swap real em mainnet - trancado por BSC_PERMITIR_ENVIO_REAL."""
    try:
        import wallet_bsc
        conta = wallet_bsc.obter_conta()
        de = conta.address
        router = to_checksum_address(config.PANCAKE_ROUTER)

        # 1) A PancakeSwap so pode mover os teus tokens se tiver "allowance".
        #    Verificamos; se for menos do que vamos vender, fazemos approve.
        dados_allow = _SEL_ALLOWANCE + encode(["address", "address"],
                                              [to_checksum_address(de), router])
        allow_hex = _rpc("eth_call", [{"to": to_checksum_address(mint),
                                       "data": "0x" + dados_allow.hex()}, "latest"])
        allowance = int(allow_hex, 16)

        precisa_approve = allowance < quantidade

        # 2) amountOutMin com folga de slippage
        folga = 1 - (config.SLIPPAGE_BPS / 10_000)
        amount_out_min = int(bnb_wei * folga)
        prazo = int(time.time()) + 120

        # Trava final: nada e enviado sem a flag explicita
        if not config.BSC_PERMITIR_ENVIO_REAL:
            return {"sucesso": False, "dry_run": False, "chain": "bsc",
                    "mensagem": ("[BSC] venda pronta, mas envio real bloqueado "
                                 "(BSC_PERMITIR_ENVIO_REAL=false). Nada foi vendido.")}

        # approve "infinito" (padrao) para nao repetir em vendas futuras
        if precisa_approve:
            dados_approve = _SEL_APPROVE + encode(["address", "uint256"], [router, _MAX_UINT])
            _enviar_tx(conta, {"to": to_checksum_address(mint), "value": 0,
                               "data": "0x" + dados_approve.hex()})

        # swapExactTokensForETHSupportingFeeOnTransferTokens (aguenta tokens com taxa)
        dados_swap = _SEL_SWAP_TOKENS_FOR_ETH + encode(
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [quantidade, amount_out_min, [to_checksum_address(mint), WBNB],
             to_checksum_address(de), prazo],
        )
        tx_hash = _enviar_tx(conta, {"to": router, "value": 0, "data": "0x" + dados_swap.hex()})

        import carteira
        carteira.registar_venda(
            pos["simbolo"], valor_recebido_usd, investido_proporcional, mint=mint,
            preco_compra_usd=pos.get("preco_compra_usd"), preco_venda_usd=preco_venda_usd,
            quantidade_tokens=quantidade, chain="bsc",
        )
        _fechar_ou_reduzir(mint, pos, quantidade, percentagem)
        return {"sucesso": True, "dry_run": False, "chain": "bsc", "assinatura": tx_hash,
                "mensagem": f"[BSC] Vendido {percentagem:.0f}% de {pos['simbolo']} - tx {tx_hash[:12]}..."}

    except Exception as e:
        return {"sucesso": False, "dry_run": False, "chain": "bsc",
                "mensagem": f"[BSC] falha na venda: {e}"}


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
