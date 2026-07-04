"""
main.py  -  ORQUESTRADOR do Sniper Bot Solana (Fase 1 + Fase 2)
===================================================================
Junta todas as pecas e corre o ciclo principal:

  1. Detetar pools/tokens novos                (detector.py)
  2. Analisar cada um (on-chain + heuristica)  (analyzer.py)
  3. Camada 1 de IA - DeepSeek - para todos    (ai_layer1.py)
  4. Camada 2 de IA - Claude - so na zona ambigua (ai_layer2.py)
  5. Mostrar o alerta na consola               (alerts.py)
  6. Se o score for <= SCORE_COMPRA_MAX, comprar (real ou dry-run) (executor.py)
  7. Verificar periodicamente as posicoes abertas para stop-loss /
     take-profit / trailing stop                (posicoes.py + executor.py)

Correr:  python main.py     (Ctrl+C para parar)
"""

import sys
import time

import config
import detector
import analyzer
import alerts
import ai_layer1
import ai_layer2
import executor
import posicoes
import radar
import watchlist
import cooldown
import momentum


def avaliar_com_ia(dados: dict) -> dict:
    """Decide o score final combinando heuristica + Camada 1 + (talvez) Camada 2.

    Devolve o dicionario 'analise_ia' que o alerts.py sabe mostrar:
      {
        "score_final": int,
        "fonte_score": "heuristico" | "DeepSeek" | "Claude",
        "camada1": {"score","justificacao"} | None,
        "camada2": {"score","justificacao"} | None,
      }
    """
    resultado = {
        "score_final": dados["score_heuristico"],
        "fonte_score": "heuristico",
        "camada1": None,
        "camada2": None,
    }

    # ---------- CAMADA 1 (DeepSeek) - corre para TODOS ----------
    if ai_layer1.esta_configurada():
        try:
            c1 = ai_layer1.analisar_token(dados)
            resultado["camada1"] = c1
            resultado["score_final"] = c1["score"]
            resultado["fonte_score"] = "DeepSeek"
        except RuntimeError as e:
            alerts.info(f"[yellow]Camada 1 falhou, uso heuristico:[/yellow] {e}")

    # ---------- CAMADA 2 (Claude) - so na zona ambigua ----------
    score_c1 = resultado["camada1"]["score"] if resultado["camada1"] else None
    na_zona_ambigua = (
        score_c1 is not None
        and config.ZONA_AMBIGUA_MIN <= score_c1 <= config.ZONA_AMBIGUA_MAX
    )

    if na_zona_ambigua and ai_layer2.esta_configurada():
        try:
            c2 = ai_layer2.analisar_token(dados)
            resultado["camada2"] = c2
            resultado["score_final"] = c2["score"]
            resultado["fonte_score"] = "Claude"
        except RuntimeError as e:
            alerts.info(f"[yellow]Camada 2 falhou, mantenho Camada 1:[/yellow] {e}")

    return resultado


# Cache do preco do SOL: o preco quase nao mexe em poucos segundos, mas
# obter_preco_sol_usd() e chamado em cada compra (caminho critico). Sem
# cache, compras seguidas pagavam cada uma uma ida a Jupiter (~100-300ms).
# Guardamos o ultimo valor por PRECO_SOL_CACHE_SEGUNDOS para tirar essa
# latencia do caminho de execucao sem arriscar um preco desatualizado.
_preco_sol_cache: dict = {"valor": 0.0, "quando": 0.0}


def obter_preco_sol_usd(forcar: bool = False) -> float:
    """Preco atual do SOL em USD, via cotacao Jupiter (SOL -> USDC).

    Usa uma cache curta (config.PRECO_SOL_CACHE_SEGUNDOS). Passa
    forcar=True para ignorar a cache e ir buscar fresco (ex: antes de
    fechar uma posicao, onde queremos o preco mais atual possivel)."""
    agora = time.time()
    if (
        not forcar
        and _preco_sol_cache["valor"] > 0
        and (agora - _preco_sol_cache["quando"]) < config.PRECO_SOL_CACHE_SEGUNDOS
    ):
        return _preco_sol_cache["valor"]
    cot = executor._obter_cotacao(config.MINT_SOL, config.MINT_USDC, 1_000_000_000)
    preco = float(cot["outAmount"]) / 1_000_000  # USDC tem 6 casas decimais
    if preco > 0:
        _preco_sol_cache["valor"] = preco
        _preco_sol_cache["quando"] = agora
    return preco


def _e_pumpfun_curva(dados: dict) -> bool:
    """True se o token vem do pump.fun (candidato a compra na bonding
    curve, antes de graduar). O detector marca o dex como 'pump-fun'."""
    return "pump" in (dados.get("dex") or "").lower()


def tentar_comprar_curva(dados: dict, analise_ia: dict) -> bool:
    """Tenta comprar um token AINDA na bonding curve do pump.fun.

    So corre se TUDO isto for verdade (caso contrario devolve False e o
    fluxo normal segue):
      - o toggle PUMPFUN_BONDING_CURVE_ATIVO esta ligado
      - o token e do pump.fun
      - o score passa o limiar APERTADO da curva (mais exigente)
      - ja passou o atraso minimo desde o lancamento (evita o instante
        inicial, onde estao os piores scams)
    Usa o limite de trade proprio da curva (mais baixo). Nunca rebenta.
    """
    if not config.PUMPFUN_BONDING_CURVE_ATIVO or not _e_pumpfun_curva(dados):
        return False

    score = analise_ia["score_final"]
    simbolo = dados["token_simbolo"]

    # Limiar de score proprio, mais apertado que o das compras normais
    if score > config.PUMPFUN_SCORE_COMPRA_MAX:
        alerts.info(
            f"[dim][curva] {simbolo} score {score} > limiar apertado "
            f"{config.PUMPFUN_SCORE_COMPRA_MAX} - nao compra na curva[/dim]"
        )
        return True  # era candidato de curva, mas rejeitado: NAO cai no fluxo normal

    # Atraso minimo: usa a idade do pool como proxy do tempo desde o lancamento
    idade_seg = dados.get("idade_minutos", 0) * 60
    if idade_seg < config.PUMPFUN_ATRASO_MINIMO_SEGUNDOS:
        alerts.info(
            f"[dim][curva] {simbolo} demasiado recente "
            f"({idade_seg:.0f}s < {config.PUMPFUN_ATRASO_MINIMO_SEGUNDOS}s) - espera[/dim]"
        )
        return True

    mint = dados["token_mint"]
    if mint in posicoes.listar_posicoes_abertas():
        return True

    if config.DRY_RUN:
        import carteira
        if carteira.saldo_disponivel() < config.PUMPFUN_MAX_TRADE_USD:
            return True

    try:
        import executor_pumpfun
        preco_sol_usd = obter_preco_sol_usd()
        r = executor_pumpfun.comprar_na_curva(
            mint=mint, simbolo=simbolo,
            valor_usd=config.PUMPFUN_MAX_TRADE_USD, preco_sol_usd=preco_sol_usd,
        )
        cor = "green" if r.get("sucesso") else "yellow"
        alerts.info(f"[{cor}]{r['mensagem']}[/{cor}]")
    except Exception as e:
        alerts.info(f"[red]Falha na compra na curva de {simbolo}:[/red] {e}")
    return True  # tratado pelo caminho da curva, nao cai no fluxo normal


def _passa_checklist_caveira(dados: dict) -> tuple[bool, str]:
    """Checklist BINARIO do modo caveira - substitui o score heuristico
    neste modo. Devolve (passou, motivo). Compra SS E SO SS TODAS estas
    forem verdadeiras (sem pontuacao, so sim/nao):

      1. Mint authority revogada (None)
      2. Freeze authority revogada (None)
      3. Liquidez >= LIQUIDEZ_MINIMA_CAVEIRA_USD
      4. Idade do token <= IDADE_MAXIMA_CAVEIRA_SEGUNDOS

    Se qualquer uma falhar -> nao compra neste modo (o motivo diz qual).
    Nota: se os dados on-chain nao estiverem disponiveis (ex: RPC falhou),
    NAO conseguimos garantir 1) e 2), por isso reprovamos por seguranca -
    este modo so entra quando tem a certeza que as autoridades estao ok.
    """
    if not dados.get("onchain_disponivel"):
        return False, "dados on-chain indisponiveis (nao da para confirmar autoridades)"
    if dados.get("mint_authority") is not None:
        return False, "mint authority ATIVA (podem imprimir mais tokens)"
    if dados.get("freeze_authority") is not None:
        return False, "freeze authority ATIVA (podem congelar carteiras)"
    if dados.get("liquidez_usd", 0) < config.LIQUIDEZ_MINIMA_CAVEIRA_USD:
        return False, (f"liquidez ${dados.get('liquidez_usd', 0):,.0f} < minimo caveira "
                       f"${config.LIQUIDEZ_MINIMA_CAVEIRA_USD:,.0f}")
    idade_seg = dados.get("idade_minutos", 999) * 60
    if idade_seg > config.IDADE_MAXIMA_CAVEIRA_SEGUNDOS:
        return False, (f"idade {idade_seg:.0f}s > maximo caveira "
                       f"{config.IDADE_MAXIMA_CAVEIRA_SEGUNDOS}s (ja nao e recente o suficiente)")
    return True, "todas as 4 condicoes verdadeiras"


def _passa_filtro_qualidade_caveira(dados: dict) -> tuple[bool, str]:
    """Filtro de QUALIDADE do Caveira (ver config.py, secao 'Filtro de
    QUALIDADE do Caveira'). Corre DEPOIS da checklist binaria rapida, e
    e deliberadamente mais lento: espera CAVEIRA_JANELA_MOMENTUM_SEGUNDOS
    para dar tempo a atividade real acontecer, depois consulta o mint via
    RPC (momentum.py) para contar compras/vendas/compradores unicos.

    Cada sinal desliga-se individualmente (valor 0, ou 100 no top-holder)
    - com TODOS desligados, esta funcao e um no-op instantaneo (mesmo
    comportamento de antes desta funcionalidade existir).

    Politica de dados em falta (decisao explicita, documentada no resumo):
    se a CHAMADA RPC falhar (disponivel=False), SALTAMOS os sinais de
    momentum (nao bloqueiam - falha de rede nao e culpa do token). Mas se
    a chamada TIVER sucesso e devolver zero atividade real (0 compradores,
    0 transacoes), isso conta a serio contra o token - e exatamente o
    sinal "nasceu e ninguem quer" que motivou este filtro.
    """
    algum_filtro_ativo = (
        config.CAVEIRA_RATIO_COMPRA_VENDA_MIN > 0
        or config.CAVEIRA_COMPRADORES_UNICOS_MIN > 0
        or config.CAVEIRA_TRANSACOES_MIN > 0
    )
    top_holder_ativo = 0 < config.CAVEIRA_TOP_HOLDER_MAX_PCT < 100

    # --- Concentracao do maior holder: reutiliza dados JA calculados pelo
    # analyzer.py (sem chamada RPC extra) - por isso corre ja, antes da
    # espera de momentum (falha rapido e barato se o holder e demasiado
    # concentrado, sem gastar a janela de espera a toa). ---
    if top_holder_ativo and dados.get("holders_disponivel"):
        top_pct = dados.get("top_holder_pct", 0.0)
        if top_pct > config.CAVEIRA_TOP_HOLDER_MAX_PCT:
            return False, (f"holder concentrado: {top_pct:.1f}% > maximo "
                           f"{config.CAVEIRA_TOP_HOLDER_MAX_PCT:.1f}%")

    if not algum_filtro_ativo:
        return True, "filtro de momentum desligado (todos os limiares a 0)"

    mint = dados["token_mint"]
    if config.CAVEIRA_JANELA_MOMENTUM_SEGUNDOS > 0:
        time.sleep(config.CAVEIRA_JANELA_MOMENTUM_SEGUNDOS)

    excluir = {dados["pool_address"]} if dados.get("pool_address") else set()
    m = momentum.analisar_momentum(mint, excluir=excluir)

    if not m["disponivel"]:
        return True, "dados de momentum indisponiveis (RPC falhou) - nao bloqueia"

    if config.CAVEIRA_TRANSACOES_MIN > 0 and m["transacoes_total"] < config.CAVEIRA_TRANSACOES_MIN:
        return False, (f"so {m['transacoes_total']} transacao(oes) desde a criacao "
                       f"(< minimo {config.CAVEIRA_TRANSACOES_MIN})")

    if config.CAVEIRA_COMPRADORES_UNICOS_MIN > 0 and m["compradores_unicos"] < config.CAVEIRA_COMPRADORES_UNICOS_MIN:
        return False, (f"so {m['compradores_unicos']} comprador(es) distinto(s) "
                       f"(< minimo {config.CAVEIRA_COMPRADORES_UNICOS_MIN})")

    if config.CAVEIRA_RATIO_COMPRA_VENDA_MIN > 0:
        # Sem vendas nenhumas e um sinal BOM (ninguem a sair) - so exigimos
        # o racio quando ja ha vendas para comparar.
        if m["vendas"] > 0:
            ratio = m["compras"] / m["vendas"]
            if ratio < config.CAVEIRA_RATIO_COMPRA_VENDA_MIN:
                return False, (f"racio compra/venda {ratio:.1f}x < minimo "
                               f"{config.CAVEIRA_RATIO_COMPRA_VENDA_MIN:.1f}x "
                               f"({m['compras']} compras vs {m['vendas']} vendas)")
        elif m["compras"] == 0:
            # Zero compras E zero vendas: sem atividade real nenhuma - o
            # caso classico do token que "nasce e morre" sem ninguem tocar.
            return False, "sem atividade de compra/venda detetada (token parado)"

    return True, (f"momentum ok: {m['compras']} compras, {m['vendas']} vendas, "
                  f"{m['compradores_unicos']} compradores distintos")


def tentar_comprar_sniper_rapido(dados: dict) -> bool:
    """MODO SNIPER RAPIDO ("modo caveira") - o mais arriscado dos 3 modos
    de compra do bot. Le config.py 7c) para o contexto completo.

    ISOLAMENTO DELIBERADO: esta funcao NUNCA olha para SCORE_COMPRA_MAX,
    MAX_TRADE_USD nem para analise_ia (a IA ainda nao correu quando isto
    e chamado). Desde a simplificacao, tambem JA NAO usa o score
    heuristico - usa uma CHECKLIST BINARIA (ver _passa_checklist_caveira)
    diretamente sobre os dados on-chain rapidos (autoridades, liquidez,
    idade), mais previsivel e rapida de avaliar. E chamada ANTES de
    avaliar_com_ia() no processar_pool, exatamente para comprar antes da
    IA (mais lenta) terminar.

    So Solana por agora (o mesmo mint tem de ser negociavel via Jupiter
    de imediato - a BSC e a bonding curve do pump.fun tem os seus
    proprios modos dedicados, propositadamente separados deste).

    Devolve True se comprou (para o processar_pool marcar a posicao e,
    mais tarde, decidir se a vende de urgencia consoante o score da IA).
    """
    if not config.SNIPER_RAPIDO_ATIVO:
        return False
    if dados.get("chain") == "bsc":
        return False  # este modo e so Solana/Jupiter, por desenho
    if not config.fase2_configurada():
        return False  # sem wallet, sem trading

    mint = dados["token_mint"]
    simbolo = dados["token_simbolo"]

    # Checklist binaria (substitui o score heuristico neste modo)
    passou, motivo = _passa_checklist_caveira(dados)
    if not passou:
        alerts.info(f"[dim][💀 sniper] {simbolo} reprovado na checklist: {motivo}[/dim]")
        return False

    if mint in posicoes.listar_posicoes_abertas():
        return False  # ja ha posicao neste token (de qualquer modo)

    valor = config.SNIPER_RAPIDO_VALOR_USD

    # Limite diario OBRIGATORIO - a principal trava deste modo (verificado
    # ANTES do filtro de qualidade, que e mais lento - nao vale a pena
    # esperar a janela de momentum so para descobrir que o limite ja bateu)
    import sniper_rapido
    if not sniper_rapido.pode_gastar(valor):
        alerts.info(
            f"[dim][💀 sniper] {simbolo} ignorado - limite diario atingido "
            f"(restam ${sniper_rapido.restante_hoje_usd():.2f})[/dim]"
        )
        return False

    # Filtro de QUALIDADE (momentum) - o mais lento dos checks deste modo
    # (pode esperar alguns segundos + chamadas RPC extra), por isso corre
    # por ultimo, so depois de todos os checks baratos terem passado.
    passou_qualidade, motivo_qualidade = _passa_filtro_qualidade_caveira(dados)
    if not passou_qualidade:
        alerts.info(f"[dim][💀 sniper] {simbolo} reprovado no filtro de qualidade: {motivo_qualidade}[/dim]")
        return False
    alerts.info(f"[dim][💀 sniper] {simbolo}: {motivo_qualidade}[/dim]")

    if config.DRY_RUN:
        import carteira
        if carteira.saldo_disponivel() < valor:
            return False

    try:
        preco_sol_usd = obter_preco_sol_usd()
        r = executor.comprar_token(mint=mint, simbolo=simbolo,
                                   valor_usd=valor, preco_sol_usd=preco_sol_usd,
                                   decimais=dados.get("decimais"),
                                   dex=dados.get("dex"), modo="sniper_rapido",
                                   pool_address=dados.get("pool_address"))
        if r.get("sucesso"):
            posicoes.atualizar_posicao(mint, sniper_rapido=True)
            if config.DRY_RUN:
                import carteira
                carteira.marcar_ultima_compra(mint, sniper_rapido=True)
            sniper_rapido.registar_gasto(valor)
            alerts.info(
                f"[bold magenta]💀 [SNIPER RAPIDO] {r['mensagem']} "
                f"(passou a checklist binaria, SEM esperar pela IA)[/bold magenta]"
            )
            return True
        alerts.info(f"[yellow]💀 [sniper] falha ao comprar {simbolo}: {r.get('mensagem')}[/yellow]")
    except Exception as e:
        alerts.info(f"[red]💀 [sniper] erro a comprar {simbolo}:[/red] {e}")
    return False


def vender_sniper_se_score_mau(dados: dict, analise_ia: dict) -> None:
    """2a camada de protecao do modo sniper: se a analise COMPLETA da IA
    (que so chega depois da compra, neste modo) disser que o token e mau
    (score_final > SNIPER_RAPIDO_SCORE_VENDA_URGENTE), vende a posicao
    imediatamente - independente das regras normais de stop-loss/take-
    profit, que so correm no proximo ciclo de verificar_posicoes().

    So mexe em posicoes marcadas 'sniper_rapido': True - nunca em
    posicoes dos outros 2 modos (isolamento deliberado)."""
    mint = dados["token_mint"]
    posicao = posicoes.listar_posicoes_abertas().get(mint)
    if not posicao or not posicao.get("sniper_rapido"):
        return  # nao e uma posicao do sniper - nada a fazer aqui

    score_final = analise_ia["score_final"]
    if score_final <= config.SNIPER_RAPIDO_SCORE_VENDA_URGENTE:
        return  # a IA nao achou mau o suficiente para vender de urgencia

    alerts.info(
        f"[bold red]💀 [SNIPER RAPIDO] IA deu score {score_final} (> "
        f"{config.SNIPER_RAPIDO_SCORE_VENDA_URGENTE}) para {posicao['simbolo']} "
        f"- venda de urgencia, ignorando as regras normais de stop-loss[/bold red]"
    )
    try:
        r = executor.vender_token(mint, 100)
        cor = "green" if r.get("sucesso") else "red"
        alerts.info(f"[{cor}]{r['mensagem']}[/{cor}]")
    except Exception as e:
        alerts.info(f"[red]💀 [sniper] falha na venda de urgencia:[/red] {e}")


def tentar_copy_trade(sinal: dict) -> None:
    """COPY TRADING - replica uma compra detetada numa carteira seguida.

    4o caminho de compra dedicado, isolado dos outros 3: NUNCA olha para
    score, MAX_TRADE_USD nem analise_ia. So Solana/Jupiter. A entrada nao
    e um pool detetado, e um sinal {mint, carteira} vindo do copy_trade.

    Verificacao MINIMA de seguranca antes de comprar: mint e freeze
    authority revogadas (o basico contra rug/congelamento). Limite diario
    OBRIGATORIO, tal como o modo sniper. Nunca deixa uma falha derrubar
    o bot."""
    if not config.COPY_TRADE_ATIVO or not config.fase2_configurada():
        return

    mint = sinal.get("mint")
    if not mint or mint in posicoes.listar_posicoes_abertas():
        return  # sem mint, ou ja ha posicao neste token (de qualquer modo)

    valor = config.COPY_TRADE_VALOR_USD

    import copy_trade
    if not copy_trade.pode_gastar(valor):
        alerts.info(
            f"[dim][COPY] limite diario atingido "
            f"(restam ${copy_trade.restante_hoje_usd():.2f})[/dim]"
        )
        return

    # Verificacao minima de seguranca on-chain: autoridades revogadas
    import rpc
    try:
        info = rpc.get_mint_info(mint)
    except Exception:
        info = None
    if not info:
        alerts.info(f"[dim][COPY] {mint[:8]}... sem dados on-chain - ignorado[/dim]")
        return
    if info.get("mint_authority") is not None or info.get("freeze_authority") is not None:
        alerts.info(f"[dim][COPY] {mint[:8]}... reprovado (mint/freeze authority ativa)[/dim]")
        return

    decimais = info.get("decimais")
    simbolo = "COPY:" + mint[:4]
    carteira_seguida = sinal.get("carteira", "?")

    if config.DRY_RUN:
        import carteira
        if carteira.saldo_disponivel() < valor:
            return

    try:
        preco_sol_usd = obter_preco_sol_usd()
        r = executor.comprar_token(mint=mint, simbolo=simbolo, valor_usd=valor,
                                   preco_sol_usd=preco_sol_usd, decimais=decimais,
                                   modo="copy_trading")
        if r.get("sucesso"):
            posicoes.atualizar_posicao(mint, copy=True, copy_carteira=carteira_seguida)
            if config.DRY_RUN:
                import carteira
                carteira.marcar_ultima_compra(mint, copy=True)
            copy_trade.registar_gasto(valor)
            alerts.info(
                f"[bold cyan][COPY] {r['mensagem']} "
                f"(copiou {carteira_seguida[:8]}...)[/bold cyan]"
            )
            return
        alerts.info(f"[yellow][COPY] falha ao comprar {mint[:8]}...: {r.get('mensagem')}[/yellow]")
    except Exception as e:
        alerts.info(f"[red][COPY] erro a comprar {mint[:8]}...:[/red] {e}")


def tentar_comprar_bsc(dados: dict, analise_ia: dict) -> None:
    """Compra na BSC (via PancakeSwap), o equivalente ao tentar_comprar da
    Solana. Precisa da wallet BSC configurada; usa o limite BSC_MAX_TRADE_USD.
    Nunca deixa uma falha derrubar o bot."""
    if not config.fase2_bsc_configurada():
        return  # sem WALLET_PRIVATE_KEY_BSC, trading BSC desligado

    score = analise_ia["score_final"]
    if score > config.SCORE_COMPRA_MAX:
        return

    # Piso de liquidez (mesma regra do caminho Solana): nao compra tokens
    # com liquidez abaixo da minima conhecida.
    liquidez = dados.get("liquidez_usd") or 0.0
    if liquidez < config.LIQUIDEZ_MINIMA_USD:
        return

    mint = dados["token_mint"]
    simbolo = dados["token_simbolo"]
    if mint in posicoes.listar_posicoes_abertas():
        return

    if config.DRY_RUN:
        import carteira
        if carteira.saldo_disponivel() < config.BSC_MAX_TRADE_USD:
            return

    try:
        import executor_bsc
        r = executor_bsc.comprar_token(mint, simbolo, valor_usd=config.BSC_MAX_TRADE_USD,
                                       dex=dados.get("dex"))
        cor = "green" if r.get("sucesso") else "yellow"
        alerts.info(f"[{cor}]{r['mensagem']}[/{cor}]")
    except Exception as e:
        alerts.info(f"[red]Falha na compra BSC de {simbolo}:[/red] {e}")


def tentar_comprar(dados: dict, analise_ia: dict) -> None:
    """Se o score final for suficientemente baixo (seguro), tenta comprar
    (real ou simulado, consoante config.DRY_RUN). Nunca deixa uma falha
    de compra derrubar o bot."""
    if not config.fase2_configurada():
        return  # sem wallet configurada, Fase 2 desligada

    # BSC: caminho de execucao proprio (PancakeSwap), separado da Solana
    if dados.get("chain") == "bsc":
        tentar_comprar_bsc(dados, analise_ia)
        return

    # Se o token e do pump.fun e o modo curva esta ligado, esse caminho
    # trata dele (compra na curva ou rejeita) - nao duplicamos com Jupiter
    if tentar_comprar_curva(dados, analise_ia):
        return

    score = analise_ia["score_final"]
    if score > config.SCORE_COMPRA_MAX:
        return  # risco demasiado alto, nao compra

    # Piso de liquidez no caminho NORMAL: so compra por aqui um token com
    # liquidez real conhecida. Sem isto, o BONUS de "LP bloqueado" do
    # pump.fun (-20) cancelava a penalizacao de liquidez baixa (+20), dando
    # score 0 e comprando tokens com liquidez $0/desconhecida (observado ao
    # vivo). Tokens pump.fun recem-nascidos sao para o modo bonding curve
    # (que le a curva on-chain), nao para este caminho.
    liquidez = dados.get("liquidez_usd") or 0.0
    if liquidez < config.LIQUIDEZ_MINIMA_USD:
        alerts.info(
            f"[dim]{dados['token_simbolo']}: liquidez ${liquidez:,.0f} < minima "
            f"${config.LIQUIDEZ_MINIMA_USD:,.0f} - nao compra no caminho normal[/dim]"
        )
        return

    mint = dados["token_mint"]
    simbolo = dados["token_simbolo"]

    # Nao compra o mesmo token duas vezes
    if mint in posicoes.listar_posicoes_abertas():
        return

    # Em dry-run, respeita o saldo virtual disponivel
    if config.DRY_RUN:
        import carteira
        if carteira.saldo_disponivel() < config.MAX_TRADE_USD:
            alerts.info(
                f"[yellow]Saldo virtual insuficiente para comprar {simbolo} "
                f"(disponivel: ${carteira.saldo_disponivel():.2f})[/yellow]"
            )
            return

    try:
        preco_sol_usd = obter_preco_sol_usd()
        resultado = executor.comprar_token(
            mint=mint, simbolo=simbolo,
            valor_usd=config.MAX_TRADE_USD, preco_sol_usd=preco_sol_usd,
            decimais=dados.get("decimais"),
            dex=dados.get("dex"), modo="normal",
            pool_address=dados.get("pool_address"),
        )
        etiqueta = "[SIMULADO]" if resultado["dry_run"] else "[REAL]"
        alerts.info(f"[green]{etiqueta} COMPRA: {resultado['mensagem']}[/green]")
    except Exception as e:
        alerts.info(f"[red]Falha na compra de {simbolo}:[/red] {e}")


def _vender_posicao(mint: str, chain: str, percentagem: float) -> dict:
    """Vende uma percentagem de uma posicao, escolhendo o executor certo
    pela chain (PancakeSwap para BSC, Jupiter para Solana)."""
    if chain == "bsc":
        import executor_bsc
        return executor_bsc.vender_token(mint, percentagem)
    return executor.vender_token(mint, percentagem)


def verificar_posicoes() -> None:
    """Percorre todas as posicoes abertas e aplica as regras de
    stop-loss / take-profit / trailing stop.

    Posicoes com 'gestao_automatica' a False sao SALTADAS (o utilizador
    desativou o acompanhamento automatico e gere-as so manualmente)."""
    abertas = posicoes.listar_posicoes_abertas()
    if not abertas:
        return

    for mint, pos in abertas.items():
        # Respeita o toggle por posicao: se o utilizador desligou o
        # acompanhamento automatico, o bot nao lhe toca (so venda manual)
        if pos.get("gestao_automatica") is False:
            continue

        chain = pos.get("chain", "solana")
        try:
            quantidade = pos["quantidade_tokens"]
            if chain == "bsc":
                # BSC: valor atual via PancakeSwap (token -> BNB -> USD)
                import executor_bsc
                valor_atual_usd = executor_bsc.valor_atual_usd(mint, quantidade)
                if valor_atual_usd is None:
                    raise RuntimeError("sem rota de venda na PancakeSwap")
                # preco por unidade MINIMA (raw) - a mesma convencao agora
                # usada em preco_compra_usd (bug corrigido: era por token
                # inteiro, desalinhado com 'quantidade' que e sempre raw)
                preco_atual = valor_atual_usd / quantidade if quantidade else 0
            else:
                # Solana: cotacao do token -> USDC, dividido pela quantidade
                cot = executor._obter_cotacao(
                    mint, config.MINT_USDC, int(quantidade)
                )
                valor_atual_usd = float(cot["outAmount"]) / 1_000_000
                preco_atual = valor_atual_usd / quantidade if quantidade else 0
        except Exception as e:
            alerts.info(f"[yellow]Nao consegui cotar {pos['simbolo']}:[/yellow] {e}")
            continue

        preco_compra = pos["preco_compra_usd"]
        if preco_compra <= 0:
            continue

        variacao_pct = (preco_atual - preco_compra) / preco_compra * 100
        posicoes.atualizar_pico(mint, preco_atual)
        pico = max(pos.get("pico_preco_usd", preco_atual), preco_atual)

        # --- STOP-LOSS ---
        if variacao_pct <= -config.STOP_LOSS_PCT:
            alerts.info(
                f"[red]STOP-LOSS disparado em {pos['simbolo']} "
                f"({variacao_pct:.1f}%). A vender tudo...[/red]"
            )
            try:
                r = _vender_posicao(mint, chain, 100)
                alerts.info(f"[red]{r['mensagem']}[/red]")
            except Exception as e:
                alerts.info(f"[red]Falha ao vender {pos['simbolo']}:[/red] {e}")
            continue

        # --- DETECAO DE REVERSAO (saida antecipada, opcional) ---
        # So Solana (momentum.py fala com o RPC da Solana); so corre se
        # ligado explicitamente - custa chamadas RPC extra POR POSICAO
        # ABERTA a cada verificacao (INTERVALO_VERIFICAR_POSICOES), por
        # isso fica desligado por defeito. Reutiliza a mesma tecnica do
        # filtro de qualidade do Caveira (momentum.py), mas aplicada a uma
        # posicao JA aberta em vez de a um candidato de compra.
        if config.DETECAO_REVERSAO_ATIVA and chain == "solana":
            excluir = {pos["pool_address"]} if pos.get("pool_address") else set()
            m = momentum.analisar_momentum(mint, excluir=excluir)
            if m["disponivel"] and m["vendas"] >= config.REVERSAO_VENDAS_MIN:
                # compras=0 com vendas suficientes = reversao maxima (so
                # gente a sair, ninguem a entrar) - racio "infinito", dispara
                ratio_venda = (m["vendas"] / m["compras"]) if m["compras"] > 0 else float("inf")
                if ratio_venda >= config.REVERSAO_RATIO_VENDA_MIN:
                    alerts.info(
                        f"[yellow]REVERSAO DE VOLUME em {pos['simbolo']} "
                        f"({m['vendas']} vendas vs {m['compras']} compras recentes) - "
                        f"a vender mais cedo, sem esperar pelo take-profit normal...[/yellow]"
                    )
                    try:
                        r = _vender_posicao(mint, chain, 100)
                        alerts.info(f"[yellow]{r['mensagem']}[/yellow]")
                    except Exception as e:
                        alerts.info(f"[red]Falha ao vender {pos['simbolo']}:[/red] {e}")
                    continue

        # --- TAKE-PROFIT (so dispara uma vez) ---
        multiplicador_atual = preco_atual / preco_compra
        if (
            not pos.get("take_profit_disparado")
            and multiplicador_atual >= config.TAKE_PROFIT_MULTIPLICADOR
        ):
            alerts.info(
                f"[green]TAKE-PROFIT disparado em {pos['simbolo']} "
                f"({multiplicador_atual:.2f}x). A vender "
                f"{config.TAKE_PROFIT_VENDER_PCT}%...[/green]"
            )
            try:
                r = _vender_posicao(mint, chain, config.TAKE_PROFIT_VENDER_PCT)
                alerts.info(f"[green]{r['mensagem']}[/green]")
                posicoes.atualizar_posicao(mint, take_profit_disparado=True)
            except Exception as e:
                alerts.info(f"[red]Falha ao vender {pos['simbolo']}:[/red] {e}")
            continue

        # --- TRAILING STOP (so depois do take-profit ja ter disparado) ---
        if pos.get("take_profit_disparado") and pico > 0:
            queda_desde_pico_pct = (pico - preco_atual) / pico * 100
            if queda_desde_pico_pct >= config.TRAILING_STOP_PCT:
                alerts.info(
                    f"[yellow]TRAILING STOP disparado em {pos['simbolo']} "
                    f"(caiu {queda_desde_pico_pct:.1f}% do pico). "
                    f"A vender o resto...[/yellow]"
                )
                try:
                    r = _vender_posicao(mint, chain, 100)
                    alerts.info(f"[yellow]{r['mensagem']}[/yellow]")
                except Exception as e:
                    alerts.info(f"[red]Falha ao vender {pos['simbolo']}:[/red] {e}")


def reavaliar_watchlist() -> None:
    """Reavalia os tokens da watchlist: preco atual e se a liquidez ainda
    esta viva (a cotacao Jupiter falhar = pool provavelmente morto/rugado).

    Corre na mesma cadencia da verificacao de posicoes. Uma falha num
    token nao impede a reavaliacao dos restantes.
    """
    registos = watchlist.carregar_watchlist()
    if not registos:
        return

    # Preco do SOL so e preciso para os tokens Solana (calculado uma vez)
    preco_sol = None
    LAMPORTS_TESTE = 10_000_000  # 0.01 SOL da uma cotacao representativa

    for r in registos:
        mint = r.get("mint")
        if not mint:
            continue
        chain = r.get("chain", "solana")
        try:
            if chain == "bsc":
                # BSC: uma quantidade simbolica do token -> ha rota na PancakeSwap?
                import executor_bsc
                valor = executor_bsc.valor_atual_usd(mint, 10**18)  # 1 token (18 dec)
                if valor and valor > 0:
                    watchlist.atualizar_reavaliacao(mint, valor, liquidez_viva=True)
                else:
                    watchlist.atualizar_reavaliacao(mint, None, liquidez_viva=False)
            else:
                if preco_sol is None:
                    preco_sol = obter_preco_sol_usd()
                cot = executor._obter_cotacao(config.MINT_SOL, mint, LAMPORTS_TESTE)
                tokens_recebidos = float(cot.get("outAmount", 0))
                if tokens_recebidos > 0:
                    preco_unitario = (0.01 * preco_sol) / tokens_recebidos
                    watchlist.atualizar_reavaliacao(mint, preco_unitario, liquidez_viva=True)
                else:
                    watchlist.atualizar_reavaliacao(mint, None, liquidez_viva=False)
        except Exception:
            # Sem rota de troca -> liquidez provavelmente morta/rugada
            watchlist.atualizar_reavaliacao(mint, None, liquidez_viva=False)
        time.sleep(0.3)  # pausa curta para nao martelar as APIs


def processar_pool(pool: dict) -> None:
    """Trata um pool novo do inicio ao fim: analisar -> IA -> alerta -> compra.

    A analise on-chain e escolhida pela CHAIN do pool: Solana usa o
    analyzer.py (mint/freeze/holders via RPC); BSC usa o analyzer_bsc.py
    (honeypot/taxas via Honeypot.is). A partir daqui o fluxo e o mesmo -
    a IA, o alerta, o radar e a watchlist trabalham sobre o dict 'dados'.

    COOLDOWN DE REANALISE: o detector.py so evita repetir o mesmo POOL
    (pool_address) - mas o mesmo TOKEN pode aparecer com pools diferentes
    em pouco tempo (ex: migracao da bonding curve do pump.fun para um
    pool normal). Por isso, antes de gastar uma analise completa (e uma
    chamada a IA), verificamos se este mint ja foi analisado ha menos de
    COOLDOWN_REANALISE_MINUTOS. Excecao: se ja for uma posicao aberta,
    o cooldown NAO se aplica aqui (isso nunca bloqueia o
    verificar_posicoes(), que corre num ciclo totalmente separado).
    """
    mint_bruto = pool.get("token_mint")
    ja_e_posicao_aberta = bool(mint_bruto) and mint_bruto in posicoes.listar_posicoes_abertas()
    if mint_bruto and not ja_e_posicao_aberta and cooldown.foi_analisado_recentemente(mint_bruto):
        alerts.info(
            f"[dim]{pool.get('token_simbolo', '?')} ja foi analisado ha menos de "
            f"{config.COOLDOWN_REANALISE_MINUTOS} min - a ignorar (evita gastar IA outra vez)[/dim]"
        )
        return

    chain = pool.get("chain", "solana")
    if chain == "bsc":
        import analyzer_bsc
        dados = analyzer_bsc.analisar(pool)
    else:
        dados = analyzer.analisar_onchain(pool)
    # Enriquece 'dados' com o endereco do pool/curva (nao vem do analyzer,
    # so do detector) - usado pelo filtro de qualidade do Caveira e pela
    # deteccao de reversao (momentum.py) para saber que conta excluir da
    # contagem de compradores (o pool nao e um "comprador").
    dados["pool_address"] = pool.get("pool_address")

    # Marca este mint como "analisado agora" - so depois de decidirmos
    # mesmo prosseguir com a analise (nao antes do cooldown-check acima)
    if mint_bruto:
        try:
            cooldown.registar_analise(mint_bruto)
        except Exception as e:
            alerts.info(f"[yellow]Nao consegui registar o cooldown:[/yellow] {e}")

    # MODO SNIPER RAPIDO: corre AQUI, logo apos a analise on-chain e ANTES
    # da chamada a IA (mais lenta) - e literalmente o ponto do modo:
    # comprar antes da analise completa terminar. So usa o score
    # heuristico (dados["score_heuristico"]), nunca a IA.
    tentar_comprar_sniper_rapido(dados)

    analise_ia = avaliar_com_ia(dados)
    alerts.mostrar_alerta(dados, analise_ia)
    tentar_comprar(dados, analise_ia)

    # Se esta posicao foi comprada pelo sniper rapido e a IA (que so
    # chega agora) considerar o token mau, vende de imediato - a 2a
    # camada de protecao deste modo, depois da compra em vez de antes.
    vender_sniper_se_score_mau(dados, analise_ia)

    # Regista o token no radar (radar.json), comprado ou nao - e isto
    # que alimenta a seccao "Radar ao vivo" do dashboard. Se a posicao
    # existir agora nas posicoes abertas, e porque a compra aconteceu.
    comprado = False
    try:
        abertas = posicoes.listar_posicoes_abertas()
        comprado = dados["token_mint"] in abertas
        modo_compra = abertas.get(dados["token_mint"], {}).get("modo") if comprado else None
        radar.registar_analise(dados, analise_ia, comprado, modo=modo_compra)
    except Exception as e:
        # O radar e so informativo: uma falha aqui nunca para o bot
        alerts.info(f"[yellow]Nao consegui registar no radar:[/yellow] {e}")

    # Token "fronteira": score acima do limiar de compra mas por pouco
    # (ate SCORE_COMPRA_MAX + SCORE_WATCHLIST_MARGEM) e nao comprado ->
    # entra na watchlist para o utilizador decidir manualmente no dashboard
    try:
        score = analise_ia["score_final"]
        e_fronteira = (
            config.SCORE_COMPRA_MAX
            < score
            <= config.SCORE_COMPRA_MAX + config.SCORE_WATCHLIST_MARGEM
        )
        if e_fronteira and not comprado:
            watchlist.adicionar(dados, analise_ia)
            alerts.info(
                f"[cyan]{dados['token_simbolo']} (score {score}) entrou na "
                f"watchlist - decisao manual no dashboard[/cyan]"
            )
    except Exception as e:
        alerts.info(f"[yellow]Nao consegui atualizar a watchlist:[/yellow] {e}")


def main() -> None:
    """Arranque + ciclo infinito de monitorizacao."""
    alerts.console.rule("[bold]SNIPER BOT SOLANA[/bold]")
    alerts.console.print(config.resumo())

    if config.fase2_configurada():
        modo = "DRY-RUN (simulado)" if config.DRY_RUN else "REAL (dinheiro de verdade!)"
        alerts.console.print(f"\n[bold]Fase 2 ativa - modo: {modo}[/bold]")
        if not config.DRY_RUN:
            import wallet
            # A confirmacao interativa (input()) so faz sentido num terminal.
            # Arrancado pelo dashboard (subprocess sem TTY), o input() rebentava
            # com EOFError e o bot MORRIA logo no arranque - ou seja, o modo
            # REAL nem sequer arrancava pelo botao. Nesse caso (headless), a
            # troca para REAL ja exigiu CONFIRMO no dashboard; aqui so avisamos
            # qual wallet vai ser usada, em vez de pedir input que ninguem le.
            tem_terminal = bool(getattr(sys.stdin, "isatty", None) and sys.stdin.isatty())
            if tem_terminal:
                if not wallet.confirmar_wallet_dedicada():
                    alerts.console.print("[red]Confirmacao nao recebida. A sair.[/red]")
                    return
            else:
                try:
                    endereco = wallet.endereco_publico()
                    saldo = wallet.obter_saldo_sol()
                    alerts.console.print(
                        f"[yellow]MODO REAL (headless): wallet {endereco} "
                        f"({saldo:.4f} SOL). Confirma que e a wallet DEDICADA ao "
                        f"bot - a troca para REAL ja foi confirmada no dashboard.[/yellow]"
                    )
                except Exception as e:
                    alerts.console.print(
                        f"[red]MODO REAL mas a wallet e invalida/indisponivel ({e}). A sair.[/red]"
                    )
                    return
    else:
        alerts.console.print(
            "\n[dim]Fase 2 desligada (sem WALLET_PRIVATE_KEY). "
            "So DETECAO + ANALISE + ALERTA.[/dim]"
        )

    alerts.console.print(
        f"\n[dim]Redes ativas: {', '.join(config.REDES_ATIVAS)}. Ctrl+C para parar.[/dim]\n"
    )

    # Detetores a usar, conforme METODO_DETECCAO ("polling", "websocket"
    # ou ambos: "polling,websocket"). Todos expoem .rede e .buscar_novos(),
    # por isso o ciclo trata-os da mesma forma.
    metodos = [m.strip().lower() for m in config.METODO_DETECCAO.split(",") if m.strip()]
    detetores = []
    if "polling" in metodos or not metodos:
        # Um detector de polling por rede ativa (Solana e/ou BSC)
        detetores += [
            detector.DetectorPools(rede, emitir_no_arranque=3)
            for rede in config.REDES_ATIVAS
        ]
    if "websocket" in metodos:
        # Deteccao instantanea de pump.fun via logsSubscribe (so Solana).
        # Importado aqui para nao obrigar a ter a lib 'websockets' quando
        # nao se usa este metodo.
        try:
            import detector_websocket
            detetores.append(detector_websocket.DetectorWebsocket("solana"))
            alerts.console.print("[dim]Deteccao WebSocket (pump.fun) ativa.[/dim]")
        except Exception as e:
            alerts.info(f"[red]Nao consegui iniciar a deteccao WebSocket:[/red] {e}")
    alerts.console.print(f"[dim]Metodo(s) de deteccao: {', '.join(metodos) or 'polling'}[/dim]\n")

    # Copy Trading (opcional): monitor das carteiras seguidas, em thread
    # de fundo. So arranca com o toggle ligado, wallet configurada e pelo
    # menos uma carteira na lista. Isolado dos detetores de pools.
    copiador = None
    if config.COPY_TRADE_ATIVO and config.fase2_configurada():
        try:
            import copy_trade
            import rate_limiter
            carteiras = copy_trade.carteiras_configuradas()
            if carteiras:
                limitador = rate_limiter.RateLimiter(config.RPC_MAX_PEDIDOS_POR_SEGUNDO)
                copiador = copy_trade.CopyTrader(carteiras, limitador=limitador)
                alerts.console.print(
                    f"[dim]Copy Trading ativo: a seguir {len(carteiras)} carteira(s).[/dim]"
                )
            else:
                alerts.info("[yellow]Copy Trading ligado mas sem carteiras (COPY_TRADE_WALLETS vazio).[/yellow]")
        except Exception as e:
            alerts.info(f"[red]Nao consegui iniciar o Copy Trading:[/red] {e}")

    ciclo = 0
    ultima_verificacao_posicoes = 0.0

    while True:
        ciclo += 1

        # Junta os novos de TODAS as redes ativas neste ciclo
        novos = []
        for det in detetores:
            try:
                novos.extend(det.buscar_novos())
            except Exception as e:
                alerts.info(f"[red]Erro a detetar pools ({det.rede}):[/red] {e}")

        # Nº de posicoes abertas ANTES de processar: se crescer, e porque
        # uma compra abriu posicao neste ciclo -> verificamos ja a seguir
        # (verificacao reativa), sem esperar o intervalo normal, para o
        # stop-loss de um token que despenca logo apos a compra disparar
        # depressa. Vale para TODOS os modos de compra (normal/curva/caveira).
        posicoes_antes = len(posicoes.listar_posicoes_abertas()) if config.fase2_configurada() else 0

        # Copy Trading: replica as compras detetadas nas carteiras seguidas.
        # Caminho totalmente separado do pipeline de analise dos pools.
        if copiador is not None:
            try:
                for sinal in copiador.buscar_sinais():
                    tentar_copy_trade(sinal)
            except Exception as e:
                alerts.info(f"[red]Erro no Copy Trading:[/red] {e}")

        if not novos:
            alerts.info(f"[dim]ciclo {ciclo}: sem tokens novos. A aguardar {config.POLL_INTERVAL_SEGUNDOS}s...[/dim]")
        else:
            a_processar = novos[: config.MAX_ANALISES_POR_CICLO]
            alerts.info(f"[cyan]ciclo {ciclo}: {len(novos)} novo(s); a analisar {len(a_processar)}...[/cyan]")

            for pool in a_processar:
                try:
                    processar_pool(pool)
                except Exception as e:
                    alerts.info(f"[red]Erro a processar {pool.get('token_simbolo','?')}:[/red] {e}")
                time.sleep(config.PAUSA_ENTRE_TOKENS)

        # Verifica posicoes abertas periodicamente (independente de haver
        # tokens novos), para o stop-loss/take-profit disparar a tempo.
        # A watchlist e reavaliada na mesma cadencia (preco + liquidez viva).
        agora = time.time()
        abriu_posicao = (
            config.fase2_configurada()
            and len(posicoes.listar_posicoes_abertas()) > posicoes_antes
        )
        no_intervalo = agora - ultima_verificacao_posicoes >= config.INTERVALO_VERIFICAR_POSICOES
        if config.fase2_configurada() and (abriu_posicao or no_intervalo):
            try:
                verificar_posicoes()
            except Exception as e:
                alerts.info(f"[red]Erro a verificar posicoes:[/red] {e}")
            # A watchlist faz chamadas por token (com pausas) - so a
            # reavaliamos na cadencia normal, nunca no gatilho reativo, para
            # nao martelar as APIs sempre que uma compra abre posicao.
            if no_intervalo:
                try:
                    reavaliar_watchlist()
                except Exception as e:
                    alerts.info(f"[red]Erro a reavaliar a watchlist:[/red] {e}")
                ultima_verificacao_posicoes = agora

        # Cadencia do ciclo: com WebSocket ativo drenamos a fila depressa
        # (2s), senao anulava-se a vantagem de velocidade - o token chega
        # ao WS de imediato, mas so seria processado no proximo ciclo. Sem
        # WebSocket, mantemos o intervalo de polling normal.
        if "websocket" in metodos:
            time.sleep(2)
        else:
            time.sleep(config.POLL_INTERVAL_SEGUNDOS)


def _sair_limpo(signum, frame):
    """Transforma o SIGTERM (enviado pelo botao 'Parar Bot' do dashboard)
    num KeyboardInterrupt - exatamente o mesmo caminho de saida limpa
    do Ctrl+C no terminal."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _sair_limpo)
    try:
        main()
    except KeyboardInterrupt:
        alerts.console.print("\n[bold]Bot parado pelo utilizador. Ate a proxima![/bold]")

