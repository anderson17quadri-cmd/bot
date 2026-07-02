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


def tentar_comprar(dados: dict, analise_ia: dict) -> None:
    """Se o score final for suficientemente baixo (seguro), tenta comprar
    (real ou simulado, consoante config.DRY_RUN). Nunca deixa uma falha
    de compra derrubar o bot."""
    if not config.fase2_configurada():
        return  # sem wallet configurada, Fase 2 desligada

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


def processar_pool(pool: dict) -> None:
    """Trata um pool novo do inicio ao fim: analisar -> IA -> alerta -> compra."""
    dados = analyzer.analisar_onchain(pool)
    analise_ia = avaliar_com_ia(dados)
    alerts.mostrar_alerta(dados, analise_ia)
    tentar_comprar(dados, analise_ia)

    # Regista o token no radar (radar.json), comprado ou nao - e isto
    # que alimenta a seccao "Radar ao vivo" do dashboard. Se a posicao
    # existir agora nas posicoes abertas, e porque a compra aconteceu.
    try:
        comprado = dados["token_mint"] in posicoes.listar_posicoes_abertas()
        radar.registar_analise(dados, analise_ia, comprado)
    except Exception as e:
        # O radar e so informativo: uma falha aqui nunca para o bot
        alerts.info(f"[yellow]Nao consegui registar no radar:[/yellow] {e}")


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

    alerts.console.print("\n[dim]Ctrl+C para parar.[/dim]\n")

    det = detector.DetectorPools(emitir_no_arranque=3)
    ciclo = 0
    ultima_verificacao_posicoes = 0.0

    while True:
        ciclo += 1
        try:
            novos = det.buscar_novos()
        except Exception as e:
            alerts.info(f"[red]Erro a detetar pools:[/red] {e}")
            time.sleep(config.POLL_INTERVAL_SEGUNDOS)
            continue

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
        # tokens novos), para o stop-loss/take-profit disparar a tempo
        agora = time.time()
        if config.fase2_configurada() and (
            agora - ultima_verificacao_posicoes >= config.INTERVALO_VERIFICAR_POSICOES
        ):
            try:
                verificar_posicoes()
            except Exception as e:
                alerts.info(f"[red]Erro a verificar posicoes:[/red] {e}")
            ultima_verificacao_posicoes = agora

        time.sleep(config.POLL_INTERVAL_SEGUNDOS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        alerts.console.print("\n[bold]Bot parado pelo utilizador. Ate a proxima![/bold]")

