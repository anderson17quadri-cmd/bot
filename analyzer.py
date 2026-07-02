"""
analyzer.py
===========
Junta tudo o que sabemos sobre um token novo e produz:
  1) um dicionario "dados_token" limpo (que sera enviado as camadas de IA)
  2) um "score heuristico" 0-100 calculado por regras simples (sem IA)

Convencao do score:  0 = parece seguro   ...   100 = muitas red flags.

Fontes de dados:
  - detector.py  -> liquidez, dex, idade, simbolo, mint do token
  - rpc.py       -> autoridades (mint/freeze), supply, distribuicao de holders

Filosofia: NUNCA inventar. Se um dado on-chain nao estiver disponivel
(ex: RPC publico bloqueia holders), marcamos como "indisponivel" e NAO
fingimos que esta tudo bem. A IA recebe essa nuance.
"""

import config
import rpc


def _calcular_score(dados: dict) -> tuple[int, list[str]]:
    """Aplica as regras de risco e devolve (score, lista_de_fatores).

    Cada regra que dispara SOMA risco e acrescenta uma frase explicativa
    a lista de fatores (para o alerta e para a IA perceberem o porque).
    """
    score = 0
    fatores: list[str] = []

    # --- Autoridades do mint (so avaliamos se conseguimos ler on-chain) ---
    if dados["onchain_disponivel"]:
        # Freeze authority ativa = podem CONGELAR a tua carteira (grave!)
        if dados["freeze_authority"] is not None:
            score += config.PESO_FREEZE_AUTHORITY
            fatores.append("Freeze authority ATIVA (podem congelar carteiras)")
        else:
            fatores.append("Freeze authority revogada (bom sinal)")

        # Mint authority ativa = podem IMPRIMIR mais tokens (diluir-te)
        if dados["mint_authority"] is not None:
            score += config.PESO_MINT_AUTHORITY
            fatores.append("Mint authority ATIVA (podem imprimir mais tokens)")
        else:
            fatores.append("Mint authority revogada (bom sinal)")
    else:
        # Nao conseguimos ler -> nao penalizamos, mas registamos a incerteza
        fatores.append("Autoridades on-chain INDISPONIVEIS (nao foi possivel ler)")

    # --- Concentracao de holders (so se a leitura funcionou) ---
    if dados["holders_disponivel"]:
        top = dados["top_holder_pct"]
        if top > config.LIMITE_HOLDER_ALTO:
            score += config.PESO_HOLDER_ALTO
            fatores.append(f"Holder muito concentrado: {top:.1f}% num so endereco")
        elif top > config.LIMITE_HOLDER_MEDIO:
            score += config.PESO_HOLDER_MEDIO
            fatores.append(f"Concentracao media: {top:.1f}% no maior holder")
        else:
            fatores.append(f"Distribuicao ok: maior holder tem {top:.1f}%")
    else:
        fatores.append("Distribuicao de holders INDISPONIVEL (RPC publico limita)")

    # --- Liquidez ---
    liq = dados["liquidez_usd"]
    if liq < config.LIQUIDEZ_MINIMA_USD:
        score += config.PESO_LIQUIDEZ_BAIXA
        fatores.append(f"Liquidez baixa: ${liq:,.0f} (< ${config.LIQUIDEZ_MINIMA_USD:,.0f})")
    elif liq < config.LIQUIDEZ_MINIMA_USD * 2:
        score += config.PESO_LIQUIDEZ_MEDIA
        fatores.append(f"Liquidez media: ${liq:,.0f}")
    else:
        fatores.append(f"Liquidez razoavel: ${liq:,.0f}")

    # Garantir que o score fica sempre entre 0 e 100
    score = max(0, min(100, score))
    return score, fatores


def analisar_onchain(pool_info: dict) -> dict:
    """Recebe um pool (do detector) e devolve o dicionario 'dados_token' completo.

    Este dicionario e depois:
      - enviado as camadas de IA (ai_layer1 / ai_layer2)
      - usado pelo alerts.py para mostrar o alerta
    """
    mint = pool_info["token_mint"]

    # -------- 1) Ler autoridades + supply do mint (dados criticos) --------
    onchain_disponivel = False
    mint_authority = None
    freeze_authority = None
    supply = None
    decimais = None
    try:
        info = rpc.get_mint_info(mint)
        if info is not None:
            onchain_disponivel = True
            mint_authority = info["mint_authority"]
            freeze_authority = info["freeze_authority"]
            supply = info["supply"]
            decimais = info["decimais"]
    except rpc.RPCRateLimit:
        # RPC recusou por excesso de pedidos -> seguimos sem estes dados
        onchain_disponivel = False
    except rpc.RPCError:
        onchain_disponivel = False

    # -------- 2) Distribuicao de holders (best-effort) --------
    holders_disponivel = False
    top_holders: list[dict] = []
    top_holder_pct = 0.0
    if onchain_disponivel:  # so tentamos se ja conseguimos falar com o RPC
        try:
            top_holders = rpc.get_maiores_holders(mint, supply=supply, limite=5)
            if top_holders:
                holders_disponivel = True
                top_holder_pct = max(h["pct"] for h in top_holders)
        except rpc.RPCRateLimit:
            holders_disponivel = False
        except rpc.RPCError:
            holders_disponivel = False

    # -------- 3) Montar o dicionario de dados do token --------
    dados = {
        # Identificacao
        "token_simbolo": pool_info["token_simbolo"],
        "token_mint": mint,
        "dex": pool_info["dex"],
        "nome_par": pool_info["nome_par"],
        # Mercado
        "liquidez_usd": pool_info["liquidez_usd"],
        "fdv_usd": pool_info.get("fdv_usd"),
        "idade_minutos": pool_info["idade_minutos"],
        # On-chain
        "onchain_disponivel": onchain_disponivel,
        "mint_authority": mint_authority,
        "freeze_authority": freeze_authority,
        "supply": supply,
        "decimais": decimais,
        # Holders
        "holders_disponivel": holders_disponivel,
        "top_holders": top_holders,
        "top_holder_pct": round(top_holder_pct, 2),
    }

    # -------- 4) Calcular score heuristico --------
    score, fatores = _calcular_score(dados)
    dados["score_heuristico"] = score
    dados["fatores_risco"] = fatores

    return dados


# --------------------------------------------------------------------------
# Teste rapido:  python analyzer.py
# Vai buscar 1 pool real (detector) e faz a analise completa.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import detector

    print("A buscar 1 pool real para analisar...\n")
    pools = detector.buscar_pools_crus()
    if not pools:
        print("Nenhum pool devolvido pela API.")
        raise SystemExit(0)

    alvo = pools[0]
    print(f"Token: {alvo['token_simbolo']}  ({alvo['token_mint']})")
    print("A analisar (pode demorar por causa dos limites do RPC)...\n")

    dados = analisar_onchain(alvo)

    print("== DADOS DO TOKEN ==")
    print(json.dumps(dados, indent=2, ensure_ascii=False))
    print(f"\n>> SCORE HEURISTICO: {dados['score_heuristico']}/100")
    print(">> FATORES:")
    for f in dados["fatores_risco"]:
        print(f"   - {f}")
