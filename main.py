"""
main.py  -  ORQUESTRADOR do Sniper Bot Solana (Fase 1: modo alerta)
===================================================================
Junta todas as pecas e corre o ciclo principal:

  1. Detetar pools/tokens novos                (detector.py)
  2. Analisar cada um (on-chain + heuristica)  (analyzer.py)
  3. Camada 1 de IA - DeepSeek - para todos    (ai_layer1.py)
  4. Camada 2 de IA - Claude - so na zona ambigua (ai_layer2.py)
  5. Mostrar o alerta na consola               (alerts.py)

NAO executa trades. So deteta, analisa e alerta.

Correr:  python main.py     (Ctrl+C para parar)
"""

import time

import config
import detector
import analyzer
import alerts
import ai_layer1
import ai_layer2


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
    # Ponto de partida: o score heuristico (funciona sempre, mesmo sem IA)
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
            # Falhou a chamada -> ficamos com o score heuristico e avisamos
            alerts.info(f"[yellow]Camada 1 falhou, uso heuristico:[/yellow] {e}")

    # ---------- CAMADA 2 (Claude) - so na zona ambigua ----------
    # So faz sentido se a Camada 1 correu e o seu score ficou "no meio".
    score_c1 = resultado["camada1"]["score"] if resultado["camada1"] else None
    na_zona_ambigua = (
        score_c1 is not None
        and config.ZONA_AMBIGUA_MIN <= score_c1 <= config.ZONA_AMBIGUA_MAX
    )

    if na_zona_ambigua and ai_layer2.esta_configurada():
        try:
            c2 = ai_layer2.analisar_token(dados)
            resultado["camada2"] = c2
            resultado["score_final"] = c2["score"]   # a 2a validacao tem a ultima palavra
            resultado["fonte_score"] = "Claude"
        except RuntimeError as e:
            alerts.info(f"[yellow]Camada 2 falhou, mantenho Camada 1:[/yellow] {e}")

    return resultado


def processar_pool(pool: dict) -> None:
    """Trata um pool novo do inicio ao fim: analisar -> IA -> alerta."""
    # 1) Analise on-chain + score heuristico
    dados = analyzer.analisar_onchain(pool)
    # 2) Camadas de IA (com fallback gracioso)
    analise_ia = avaliar_com_ia(dados)
    # 3) Mostrar alerta
    alerts.mostrar_alerta(dados, analise_ia)


def main() -> None:
    """Arranque + ciclo infinito de monitorizacao."""
    alerts.console.rule("[bold]SNIPER BOT SOLANA - FASE 1 (modo alerta)[/bold]")
    alerts.console.print(config.resumo())
    alerts.console.print(
        "\n[dim]So DETECAO + ANALISE + ALERTA. Nao executa trades. "
        "Ctrl+C para parar.[/dim]\n"
    )

    det = detector.DetectorPools(emitir_no_arranque=3)
    ciclo = 0

    while True:
        ciclo += 1
        try:
            novos = det.buscar_novos()
        except Exception as e:
            # Rede da API de deteccao falhou -> espera e tenta outra vez
            alerts.info(f"[red]Erro a detetar pools:[/red] {e}")
            time.sleep(config.POLL_INTERVAL_SEGUNDOS)
            continue

        if not novos:
            alerts.info(f"[dim]ciclo {ciclo}: sem tokens novos. A aguardar {config.POLL_INTERVAL_SEGUNDOS}s...[/dim]")
        else:
            # Limitar quantos analisamos por ciclo (protege o RPC publico)
            a_processar = novos[: config.MAX_ANALISES_POR_CICLO]
            alerts.info(f"[cyan]ciclo {ciclo}: {len(novos)} novo(s); a analisar {len(a_processar)}...[/cyan]")

            for pool in a_processar:
                try:
                    processar_pool(pool)
                except Exception as e:
                    # Um token com problema nao pode derrubar o bot todo
                    alerts.info(f"[red]Erro a processar {pool.get('token_simbolo','?')}:[/red] {e}")
                # Pausa curta entre tokens (educado com RPC/APIs)
                time.sleep(config.PAUSA_ENTRE_TOKENS)

        # Espera ate ao proximo ciclo de deteccao
        time.sleep(config.POLL_INTERVAL_SEGUNDOS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C -> saida limpa, sem "stack trace" feio
        alerts.console.print("\n[bold]Bot parado pelo utilizador. Ate a proxima![/bold]")
