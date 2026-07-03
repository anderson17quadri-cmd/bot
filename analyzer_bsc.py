"""
analyzer_bsc.py  -  Analise de risco para tokens BSC (EVM)
============================================================
O equivalente ao analyzer.py, mas para a Binance Smart Chain. Na BSC os
riscos sao diferentes dos da Solana:
  - HONEYPOT: o contrato deixa COMPRAR mas nao deixa VENDER (prendes o
    dinheiro). E o golpe mais comum e o mais grave.
  - Taxa de venda excessiva: vendes e o contrato fica com 20-100%.
  - Ownership nao renunciado: o dono pode mudar as regras a qualquer hora.

Em vez de lermos o bytecode a mao (complexo e fragil), usamos a API
GRATUITA da Honeypot.is, que SIMULA uma compra e venda reais num fork da
chain e diz-nos: e honeypot? qual a taxa de compra/venda? quantos
holders conseguiram vender? Isto e muito mais fiavel do que heuristicas
sobre o bytecode.

Produz um dicionario 'dados' com a MESMA forma do analyzer.py da Solana
(para o alerts.py, o radar e a IA funcionarem igual), com campos extra
especificos de BSC (honeypot, buy_tax, sell_tax).

Tolerante a falha: se a Honeypot.is estiver em baixo ou nao souber o
token, devolvemos 'analise_indisponivel' e NAO penalizamos - o bot
segue. Nunca levanta excecao para fora.
"""

import requests

import config

HONEYPOT_URL = "https://api.honeypot.is/v2/IsHoneypot"
BSC_CHAIN_ID = 56

# Pesos do score BSC (0 = seguro, 100 = perigoso). Honeypot domina tudo.
PESO_HONEYPOT = 100          # nao deixa vender -> risco maximo, sempre
PESO_SELL_TAX_ALTA = 40      # taxa de venda >= LIMITE_SELL_TAX_ALTA
PESO_SELL_TAX_MEDIA = 20     # taxa de venda >= LIMITE_SELL_TAX_MEDIA
PESO_LIQUIDEZ_BAIXA = 20     # abaixo da liquidez minima
PESO_HOLDERS_FALHAM = 25     # muitos holders NAO conseguem vender (siphoned)

LIMITE_SELL_TAX_ALTA = 20.0  # %
LIMITE_SELL_TAX_MEDIA = 10.0  # %


def _consultar_honeypot(endereco: str) -> dict | None:
    """Chama a Honeypot.is para um contrato BSC. Devolve o JSON ou None
    se qualquer coisa falhar (rede, token desconhecido, resposta ma)."""
    try:
        resposta = requests.get(HONEYPOT_URL, params={
            "address": endereco, "chainID": BSC_CHAIN_ID,
        }, timeout=15)
        resposta.raise_for_status()
        return resposta.json()
    except Exception:
        return None


def analisar(pool_info: dict) -> dict:
    """Recebe um pool BSC (do detector) e devolve o dicionario 'dados'
    completo, com score_heuristico e fatores_risco - pronto para a IA,
    o alerta e o radar, tal como o analyzer.py da Solana."""
    endereco = pool_info["token_mint"]  # na BSC e o contrato 0x...
    hp = _consultar_honeypot(endereco)

    score = 0
    fatores: list[str] = []

    # Campos comuns (existem em qualquer chain)
    dados = {
        "chain": "bsc",
        "token_simbolo": pool_info["token_simbolo"],
        "token_mint": endereco,
        "dex": pool_info["dex"],
        "nome_par": pool_info["nome_par"],
        "liquidez_usd": pool_info["liquidez_usd"],
        "fdv_usd": pool_info.get("fdv_usd"),
        "idade_minutos": pool_info["idade_minutos"],
        # Campos especificos de BSC (default = desconhecido)
        "analise_indisponivel": hp is None,
        "honeypot": None,
        "buy_tax": None,
        "sell_tax": None,
        "ownership_renunciada": None,
        "holders_total": None,
        "holders_falharam": None,
        # Campos Solana-only que o resto do codigo espera - marcados N/A
        "onchain_disponivel": False,
        "mint_authority": None,
        "freeze_authority": None,
        "supply": None,
        "holders_disponivel": False,
        "top_holder_pct": 0.0,
        "top5_holders_pct": 0.0,
        "liquidez_bloqueada": "desconhecido",
        "deployer_tokens_criados": None,
        "liquidez_suspeita": False,
    }

    # --- Se a Honeypot.is nao respondeu: nao penaliza, so regista ---
    if hp is None:
        fatores.append("Analise Honeypot.is INDISPONIVEL (nao penaliza)")
        # Ainda avaliamos a liquidez (esse dado vem do detector, nao da API)
        dados["score_heuristico"] = _pontuar_liquidez(pool_info["liquidez_usd"], fatores, dados)
        dados["fatores_risco"] = fatores
        return dados

    # --- Honeypot? (o sinal decisivo) ---
    hp_res = hp.get("honeypotResult", {})
    e_honeypot = bool(hp_res.get("isHoneypot"))
    dados["honeypot"] = e_honeypot
    if e_honeypot:
        score += PESO_HONEYPOT
        motivo = hp_res.get("honeypotReason") or "nao deixa vender"
        fatores.append(f"HONEYPOT DETETADO: {motivo} (nao consegues vender!)")

    # --- Taxas de compra/venda (simuladas na chain) ---
    sim = hp.get("simulationResult", {})
    buy_tax = _num(sim.get("buyTax"))
    sell_tax = _num(sim.get("sellTax"))
    dados["buy_tax"] = buy_tax
    dados["sell_tax"] = sell_tax
    if sell_tax is not None:
        if sell_tax >= LIMITE_SELL_TAX_ALTA:
            score += PESO_SELL_TAX_ALTA
            fatores.append(f"Taxa de venda MUITO ALTA: {sell_tax:.1f}%")
        elif sell_tax >= LIMITE_SELL_TAX_MEDIA:
            score += PESO_SELL_TAX_MEDIA
            fatores.append(f"Taxa de venda elevada: {sell_tax:.1f}%")
        else:
            fatores.append(f"Taxa de venda ok: {sell_tax:.1f}% (compra: {buy_tax or 0:.1f}%)")

    # --- Holders que falharam a venda (sinal de honeypot "parcial") ---
    ha = hp.get("holderAnalysis", {})
    total = _num(ha.get("holders"))
    falhados = _num(ha.get("failed"))
    dados["holders_total"] = int(total) if total is not None else None
    dados["holders_falharam"] = int(falhados) if falhados is not None else None
    if total and total > 20 and falhados and (falhados / total) > 0.2:
        score += PESO_HOLDERS_FALHAM
        fatores.append(f"{falhados:.0f}/{total:.0f} holders NAO conseguiram vender (>20%)")

    # --- Liquidez ---
    score += _pontuar_liquidez(pool_info["liquidez_usd"], fatores, dados)

    dados["score_heuristico"] = max(0, min(100, score))
    dados["fatores_risco"] = fatores
    return dados


def _pontuar_liquidez(liq: float, fatores: list, dados: dict) -> int:
    """Pontua a liquidez (comum aos ramos com/sem honeypot). Devolve o
    peso a somar ao score."""
    if liq < config.LIQUIDEZ_MINIMA_USD:
        fatores.append(f"Liquidez baixa: ${liq:,.0f} (< ${config.LIQUIDEZ_MINIMA_USD:,.0f})")
        return PESO_LIQUIDEZ_BAIXA
    fatores.append(f"Liquidez razoavel: ${liq:,.0f}")
    return 0


def _num(valor):
    """A Honeypot.is manda numeros as vezes como texto. Converte com
    seguranca para float, ou None se nao der."""
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Teste rapido:  python3 analyzer_bsc.py [endereco_do_contrato]
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    # CAKE (nao e honeypot) por defeito
    addr = sys.argv[1] if len(sys.argv) > 1 else "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"
    pool = {
        "token_simbolo": "TESTE", "token_mint": addr, "dex": "pancakeswap",
        "nome_par": "TESTE / BNB", "liquidez_usd": 50_000.0,
        "fdv_usd": None, "idade_minutos": 5.0,
    }
    d = analisar(pool)
    print(f"honeypot: {d['honeypot']} | buy_tax: {d['buy_tax']} | sell_tax: {d['sell_tax']}")
    print(f"score BSC: {d['score_heuristico']}/100")
    for f in d["fatores_risco"]:
        print("  -", f)
