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
        top5 = dados["top5_holders_pct"]
        if top > config.LIMITE_HOLDER_ALTO:
            score += config.PESO_HOLDER_ALTO
            fatores.append(f"Holder muito concentrado: {top:.1f}% num so endereco (top 5: {top5:.1f}%)")
        elif top > config.LIMITE_HOLDER_MEDIO:
            score += config.PESO_HOLDER_MEDIO
            fatores.append(f"Concentracao media: {top:.1f}% no maior holder (top 5: {top5:.1f}%)")
        else:
            fatores.append(f"Distribuicao ok: maior holder tem {top:.1f}% (top 5: {top5:.1f}%)")
    else:
        fatores.append("Distribuicao de holders INDISPONIVEL (falha do RPC)")

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

    # --- Liquidez bloqueada/queimada (o sinal mais forte contra rugs) ---
    estado_lp = dados["liquidez_bloqueada"]
    if estado_lp == "queimada":
        score += config.BONUS_LIQUIDEZ_BLOQUEADA  # negativo -> reduz o score
        fatores.append("LP QUEIMADO - o criador nao pode retirar a liquidez (sinal muito forte de seguranca)")
    elif estado_lp == "bloqueada_protocolo":
        score += config.BONUS_LIQUIDEZ_BLOQUEADA
        fatores.append("Liquidez gerida pelo protocolo (pump.fun/pumpswap) - sem LP para o criador sacar")
    elif estado_lp == "nao_bloqueada":
        score += config.PESO_LIQUIDEZ_NAO_BLOQUEADA
        fatores.append("LP NAO bloqueado - o criador pode retirar a liquidez a qualquer momento (risco de rug)")
    else:
        fatores.append("Estado do LP desconhecido (nao penaliza nem beneficia)")

    # --- Historico do deployer (scam em serie?) ---
    criados = dados["deployer_tokens_criados"]
    if criados is not None:
        if criados > config.LIMITE_DEPLOYER_TOKENS:
            score += config.PESO_DEPLOYER_SERIAL
            fatores.append(
                f"Deployer criou {criados} tokens nas ultimas "
                f"{config.DEPLOYER_JANELA_HORAS}h - padrao de scam em serie"
            )
        else:
            fatores.append(
                f"Deployer criou {criados} token(s) nas ultimas "
                f"{config.DEPLOYER_JANELA_HORAS}h (dentro do normal)"
            )

    # --- Liquidez suspeita para a idade (informativo, sem peso no score) ---
    # Um pool com minutos de vida e liquidez ja enorme pode ser um bot a
    # inflacionar antes de um pump artificial. E so um INDICIO (projetos
    # legitimos tambem lancam com liquidez grande), por isso nao mexe no
    # score - vai como contexto para a IA e para o alerta.
    if dados["liquidez_suspeita"]:
        fatores.append(
            f"Liquidez ja alta (${liq:,.0f}) com so {dados['idade_minutos']} min "
            f"de vida - possivel inflacao artificial (indicio, nao prova)"
        )

    # Garantir que o score fica sempre entre 0 e 100
    score = max(0, min(100, score))
    return score, fatores


def ler_autoridades_mint(mint: str) -> dict:
    """Le SO mint_authority/freeze_authority/supply/decimais (1 chamada
    RPC: rpc.get_mint_info). Extraido de analisar_onchain() para ser
    reutilizado tambem pelo caminho LEVE do Caveira (ver
    analisar_onchain_leve_caveira, mais abaixo) e pela retentativa em
    main.py (_reprocessar_caveira_pendentes) - fonte UNICA da distincao
    "rate_limit" vs "conta_ausente" vs "erro_rpc", para nao voltar a
    duplicar esta logica em 2 sitios que podiam divergir.

    Devolve sempre os mesmos 6 campos, com defeitos seguros em caso de
    falha (onchain_disponivel=False, resto None):
      onchain_disponivel, onchain_motivo_indisponivel, mint_authority,
      freeze_authority, supply, decimais.
    """
    onchain_disponivel = False
    # PORQUE ficou indisponivel, nao so QUE ficou - "rate_limit" (RPC
    # recusou por excesso de pedidos, nao prova nada sobre o token) e
    # "conta_ausente" (RPC respondeu, a conta genuinamente nao existe)
    # sao situacoes muito diferentes mas antes colapsavam ambas em
    # onchain_disponivel=False sem distincao nenhuma - impossivel depois
    # saber, so pelo log, qual delas aconteceu (ver o diagnostico do
    # token CWC em main.py, junto de _caveira_pendentes).
    onchain_motivo_indisponivel = None
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
        else:
            onchain_motivo_indisponivel = "conta_ausente"
    except rpc.RPCRateLimit:
        # RPC recusou por excesso de pedidos -> seguimos sem estes dados
        onchain_disponivel = False
        onchain_motivo_indisponivel = "rate_limit"
    except rpc.RPCError:
        onchain_disponivel = False
        onchain_motivo_indisponivel = "erro_rpc"

    return {
        "onchain_disponivel": onchain_disponivel,
        "onchain_motivo_indisponivel": onchain_motivo_indisponivel,
        "mint_authority": mint_authority,
        "freeze_authority": freeze_authority,
        "supply": supply,
        "decimais": decimais,
    }


def analisar_onchain_leve_caveira(pool_info: dict) -> dict:
    """Versao LEVE de analisar_onchain(), usada SO pelo modo Sniper
    Rapido/Caveira (main.py:tentar_comprar_sniper_rapido e
    _reprocessar_caveira_pendentes) - NUNCA por normal/curva/BSC.

    A checklist binaria do Caveira (main.py:_passa_checklist_caveira) so
    usa 4 coisas: mint_authority, freeze_authority, liquidez_usd e
    idade_minutos. As duas ultimas ja vem do detector.py, sem custo de
    RPC nenhum - so as autoridades exigem uma chamada. Chamar
    analisar_onchain() completo (que TAMBEM busca holders + sinais
    avancados - liquidez bloqueada, historico do deployer, ate 6
    chamadas RPC extra que a checklist nunca le) fazia o Caveira competir
    pelo mesmo limitador global de RPC (rpc.py:_limitador_global) com
    chamadas que o seu proprio caminho nem usa, e esperar por elas antes
    de decidir - o oposto do "comprar antes da IA/analise completa
    terminar" que e a razao de existir deste modo.

    Devolve um dict com SO os campos que tentar_comprar_sniper_rapido/
    _passa_checklist_caveira/_passa_filtro_qualidade_caveira/
    _registar_decisao_memoria precisam - holders_disponivel fica sempre
    False e top_holder_pct 0.0 (nunca inventados, e os consumidores ja
    tratam a sua ausencia com seguranca, exatamente como quando o RPC
    completo falha). NAO e um substituto geral de analisar_onchain() -
    quem comprar por este caminho continua a ser reavaliado pela analise
    completa a seguir (para a IA, o radar e a watchlist), este dict serve
    SO para a decisao rapida do Caveira.
    """
    mint = pool_info["token_mint"]
    autoridades = ler_autoridades_mint(mint)

    return {
        "token_simbolo": pool_info["token_simbolo"],
        "token_mint": mint,
        "chain": pool_info.get("chain", "solana"),
        "dex": pool_info["dex"],
        "pool_address": pool_info.get("pool_address"),
        "liquidez_usd": pool_info["liquidez_usd"],
        "idade_minutos": pool_info["idade_minutos"],
        "onchain_disponivel": autoridades["onchain_disponivel"],
        "onchain_motivo_indisponivel": autoridades["onchain_motivo_indisponivel"],
        "mint_authority": autoridades["mint_authority"],
        "freeze_authority": autoridades["freeze_authority"],
        "decimais": autoridades["decimais"],
        # Nunca calculados neste caminho leve - ausencia ja e tratada com
        # seguranca por quem le (ver docstring acima).
        "holders_disponivel": False,
        "top_holder_pct": 0.0,
        # Atividade de trading recente - vem do detector.py (GeckoTerminal),
        # sem custo de RPC nenhum, por isso inclui-se aqui tambem (so para
        # o registo em memoria - o Caveira nao usa _filtro_atividade_recente).
        "compradores_unicos": pool_info.get("compradores_unicos"),
        "vendedores_unicos": pool_info.get("vendedores_unicos"),
        "transacoes_compra": pool_info.get("transacoes_compra"),
        "transacoes_venda": pool_info.get("transacoes_venda"),
        "volume_usd_recente": pool_info.get("volume_usd_recente"),
    }


def analisar_onchain(pool_info: dict) -> dict:
    """Recebe um pool (do detector) e devolve o dicionario 'dados_token' completo.

    Este dicionario e depois:
      - enviado as camadas de IA (ai_layer1 / ai_layer2)
      - usado pelo alerts.py para mostrar o alerta
    """
    mint = pool_info["token_mint"]

    # -------- 1) Ler autoridades + supply do mint (dados criticos) --------
    autoridades = ler_autoridades_mint(mint)
    onchain_disponivel = autoridades["onchain_disponivel"]
    onchain_motivo_indisponivel = autoridades["onchain_motivo_indisponivel"]
    mint_authority = autoridades["mint_authority"]
    freeze_authority = autoridades["freeze_authority"]
    supply = autoridades["supply"]
    decimais = autoridades["decimais"]

    # -------- 2) Distribuicao de holders (uma chamada por token) --------
    # getTokenLargestAccounts devolve as 20 maiores contas do token.
    # obter_maiores_holders nunca levanta excecao: se o RPC falhar
    # (rate limit, Helius em baixo...), vem lista vazia e seguimos.
    holders_disponivel = False
    top_holders: list[dict] = []
    top_holder_pct = 0.0
    top5_holders_pct = 0.0
    if onchain_disponivel:  # so tentamos se ja conseguimos falar com o RPC
        top_holders = rpc.obter_maiores_holders(mint, supply=supply)
        if top_holders:
            holders_disponivel = True
            # A lista vem ordenada da maior conta para a mais pequena,
            # mas usamos max() por seguranca (nao custa nada)
            top_holder_pct = max(h["pct"] for h in top_holders)
            # % que os 5 maiores detem JUNTOS (visao de concentracao real:
            # 5 carteiras com 15% cada = 75%, tao mau como 1 com 75%)
            top5_holders_pct = sum(h["pct"] for h in top_holders[:5])

    # -------- 3) Sinais avancados (todos tolerantes a falha) --------
    # Sao os que mais chamadas gastam (Raydium: 3 RPC; deployer: 2 RPC + 1
    # Helius). So os corremos se ANALISE_ONCHAIN_AVANCADA estiver ligado E
    # ja tivermos conseguido ler o mint on-chain (se o RPC ja nos limitou,
    # martelar mais chamadas so piora o rate limit e falharia na mesma).
    # Desligados/indisponiveis -> ficam neutros (mais conservador: perde-se
    # o BONUS do LP bloqueado, o que SOBE o score, nunca desce).
    liquidez_bloqueada = "desconhecido"
    deployer = None
    deployer_tokens_criados = None
    if config.ANALISE_ONCHAIN_AVANCADA and onchain_disponivel:
        # 3a) Liquidez bloqueada/queimada. Para pump.fun/pumpswap nao gasta
        #     chamadas (deteta pelo dex); para Raydium v4 sao 3 chamadas RPC.
        liquidez_bloqueada = rpc.verificar_liquidez_bloqueada(
            pool_info.get("pool_address", ""), pool_info.get("dex", "")
        )
        # 3b) Historico do deployer - SO se a liquidez passou no filtro
        #     minimo (nao gastar Helius em tokens que ja iam ser descartados)
        if pool_info["liquidez_usd"] >= config.LIQUIDEZ_MINIMA_USD:
            deployer = rpc.obter_deployer(mint)
            if deployer:
                deployer_tokens_criados = rpc.contar_tokens_criados(
                    deployer, janela_horas=config.DEPLOYER_JANELA_HORAS
                )

    # 3c) Liquidez suspeita para a idade (nao gasta chamadas nenhumas)
    idade = pool_info.get("idade_minutos", -1)
    liquidez_suspeita = (
        0 <= idade < config.IDADE_SUSPEITA_MINUTOS
        and pool_info["liquidez_usd"] >= config.LIQUIDEZ_SUSPEITA_USD
    )

    # -------- 4) Montar o dicionario de dados do token --------
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
        # So preenchido quando onchain_disponivel e False - "rate_limit"
        # vs "conta_ausente" vs "erro_rpc" (ver main.py:
        # _MOTIVOS_ONCHAIN_INDISPONIVEL e o comentario acima em 1)).
        "onchain_motivo_indisponivel": onchain_motivo_indisponivel,
        "mint_authority": mint_authority,
        "freeze_authority": freeze_authority,
        "supply": supply,
        "decimais": decimais,
        # Holders
        "holders_disponivel": holders_disponivel,
        "top_holders": top_holders,
        "top_holder_pct": round(top_holder_pct, 2),
        "top5_holders_pct": round(top5_holders_pct, 2),
        # Sinais avancados
        "liquidez_bloqueada": liquidez_bloqueada,
        "deployer": deployer,
        "deployer_tokens_criados": deployer_tokens_criados,
        "liquidez_suspeita": liquidez_suspeita,
        # Atividade de trading recente (GeckoTerminal, capturada no
        # detector.py) - None quando a API nao trouxe a janela (o
        # detector.py so promete m5/m15; nunca inventa um 0). Ausente
        # tambem quando o pool veio do detector_websocket.py (esse
        # caminho nao consulta a GeckoTerminal, so decodifica o evento
        # on-chain) - o filtro em main.py trata os dois casos da mesma
        # forma (fail-open, ver _filtro_atividade_recente).
        "compradores_unicos": pool_info.get("compradores_unicos"),
        "vendedores_unicos": pool_info.get("vendedores_unicos"),
        "transacoes_compra": pool_info.get("transacoes_compra"),
        "transacoes_venda": pool_info.get("transacoes_venda"),
        "volume_usd_recente": pool_info.get("volume_usd_recente"),
    }

    # -------- 5) Calcular score heuristico --------
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
