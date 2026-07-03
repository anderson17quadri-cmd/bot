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


def obter_preco_sol_usd() -> float:
    """Preco atual do SOL em USD, via cotacao Jupiter (SOL -> USDC)."""
    cot = executor._obter_cotacao(config.MINT_SOL, config.MINT_USDC, 1_000_000_000)
    return float(cot["outAmount"]) / 1_000_000  # USDC tem 6 casas decimais


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


def tentar_comprar(dados: dict, analise_ia: dict) -> None:
    """Se o score final for suficientemente baixo (seguro), tenta comprar
    (real ou simulado, consoante config.DRY_RUN). Nunca deixa uma falha
    de compra derrubar o bot."""
    if not config.fase2_configurada():
        return  # sem wallet configurada, Fase 2 desligada

    # BSC: a execucao (PancakeSwap) e a fase B3 - ainda nao implementada.
    # Ate la, os tokens BSC entram no fluxo so ate ao alerta/radar/watchlist,
    # nunca sao comprados (o executor da Solana nao serve para EVM).
    if dados.get("chain") == "bsc":
        return

    # Se o token e do pump.fun e o modo curva esta ligado, esse caminho
    # trata dele (compra na curva ou rejeita) - nao duplicamos com Jupiter
    if tentar_comprar_curva(dados, analise_ia):
        return

    score = analise_ia["score_final"]
    if score > config.SCORE_COMPRA_MAX:
        return  # risco demasiado alto, nao compra

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
        )
        etiqueta = "[SIMULADO]" if resultado["dry_run"] else "[REAL]"
        alerts.info(f"[green]{etiqueta} COMPRA: {resultado['mensagem']}[/green]")
    except Exception as e:
        alerts.info(f"[red]Falha na compra de {simbolo}:[/red] {e}")


def verificar_posicoes() -> None:
    """Percorre todas as posicoes abertas e aplica as regras de
    stop-loss / take-profit / trailing stop."""
    abertas = posicoes.listar_posicoes_abertas()
    if not abertas:
        return

    for mint, pos in abertas.items():
        try:
            # Preco atual: cotacao do token -> USDC, dividido pela quantidade
            cot = executor._obter_cotacao(
                mint, config.MINT_USDC, int(pos["quantidade_tokens"])
            )
            valor_atual_usd = float(cot["outAmount"]) / 1_000_000
            quantidade = pos["quantidade_tokens"]
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
                r = executor.vender_token(mint, 100)
                alerts.info(f"[red]{r['mensagem']}[/red]")
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
                r = executor.vender_token(mint, config.TAKE_PROFIT_VENDER_PCT)
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
                    r = executor.vender_token(mint, 100)
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

    try:
        preco_sol = obter_preco_sol_usd()
    except Exception:
        return  # sem preco do SOL nao ha como converter; tenta no proximo ciclo

    # 0.01 SOL e suficiente para obter uma cotacao representativa
    LAMPORTS_TESTE = 10_000_000

    for r in registos:
        mint = r.get("mint")
        if not mint:
            continue
        try:
            cot = executor._obter_cotacao(config.MINT_SOL, mint, LAMPORTS_TESTE)
            tokens_recebidos = float(cot.get("outAmount", 0))
            if tokens_recebidos > 0:
                # Preco por unidade minima do token (mesma convencao das posicoes)
                valor_usd_teste = 0.01 * preco_sol
                preco_unitario = valor_usd_teste / tokens_recebidos
                watchlist.atualizar_reavaliacao(mint, preco_unitario, liquidez_viva=True)
            else:
                watchlist.atualizar_reavaliacao(mint, None, liquidez_viva=False)
        except Exception:
            # Jupiter nao cotou -> sem rota de troca -> liquidez morta/rugada
            watchlist.atualizar_reavaliacao(mint, None, liquidez_viva=False)
        time.sleep(0.3)  # pausa curta para nao martelar a API da Jupiter


def processar_pool(pool: dict) -> None:
    """Trata um pool novo do inicio ao fim: analisar -> IA -> alerta -> compra.

    A analise on-chain e escolhida pela CHAIN do pool: Solana usa o
    analyzer.py (mint/freeze/holders via RPC); BSC usa o analyzer_bsc.py
    (honeypot/taxas via Honeypot.is). A partir daqui o fluxo e o mesmo -
    a IA, o alerta, o radar e a watchlist trabalham sobre o dict 'dados'.
    """
    chain = pool.get("chain", "solana")
    if chain == "bsc":
        import analyzer_bsc
        dados = analyzer_bsc.analisar(pool)
    else:
        dados = analyzer.analisar_onchain(pool)

    analise_ia = avaliar_com_ia(dados)
    alerts.mostrar_alerta(dados, analise_ia)
    tentar_comprar(dados, analise_ia)

    # Regista o token no radar (radar.json), comprado ou nao - e isto
    # que alimenta a seccao "Radar ao vivo" do dashboard. Se a posicao
    # existir agora nas posicoes abertas, e porque a compra aconteceu.
    comprado = False
    try:
        comprado = dados["token_mint"] in posicoes.listar_posicoes_abertas()
        radar.registar_analise(dados, analise_ia, comprado)
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
            if not wallet.confirmar_wallet_dedicada():
                alerts.console.print("[red]Confirmacao nao recebida. A sair.[/red]")
                return
    else:
        alerts.console.print(
            "\n[dim]Fase 2 desligada (sem WALLET_PRIVATE_KEY). "
            "So DETECAO + ANALISE + ALERTA.[/dim]"
        )

    alerts.console.print(
        f"\n[dim]Redes ativas: {', '.join(config.REDES_ATIVAS)}. Ctrl+C para parar.[/dim]\n"
    )

    # Um detector por rede ativa (Solana e/ou BSC) - correm em paralelo,
    # cada um com o seu proprio estado de "ja vistos".
    detetores = [
        detector.DetectorPools(rede, emitir_no_arranque=3)
        for rede in config.REDES_ATIVAS
    ]
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
        if config.fase2_configurada() and (
            agora - ultima_verificacao_posicoes >= config.INTERVALO_VERIFICAR_POSICOES
        ):
            try:
                verificar_posicoes()
            except Exception as e:
                alerts.info(f"[red]Erro a verificar posicoes:[/red] {e}")
            try:
                reavaliar_watchlist()
            except Exception as e:
                alerts.info(f"[red]Erro a reavaliar a watchlist:[/red] {e}")
            ultima_verificacao_posicoes = agora

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

