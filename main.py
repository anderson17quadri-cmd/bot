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
import threading
import time
from datetime import datetime, timezone

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
import rpc
import telegram_alerts


def _resultado_heuristico(dados: dict) -> dict:
    """Analise 'analise_ia' so com o score heuristico, sem chamar
    nenhuma IA - mesma forma que avaliar_com_ia devolve, para o resto do
    pipeline (alertas, radar, watchlist, tentar_comprar) funcionar sem
    diferenciar se a IA correu ou nao. Usado quando um filtro barato
    (liquidez, honeypot, taxas, holders, idade) ja reprova o token ANTES
    de gastar uma chamada de IA - ver _passa_filtros_baratos()."""
    return {
        "score_final": dados["score_heuristico"],
        "fonte_score": "heuristico",
        # Provider REAL que respondeu (groq/deepseek/openrouter/claude/
        # heuristico) - para a memoria semanal; "fonte_score" continua a
        # ser o rotulo de display que o alerts.py ja conhece.
        "provider_ia": "heuristico",
        "camada1": None,
        "camada2": None,
    }


# Nome de display por provider real - "fonte_score" e o rotulo que o
# alerts.py mostra ("score via X"); antes de existir Groq/OpenRouter
# ficava sempre hardcoded a "DeepSeek", mesmo quando outro provider e
# que tinha respondido (bug cosmetico, corrigido aqui).
_NOMES_DISPLAY_PROVIDER = {
    "groq": "Groq",
    "deepseek": "DeepSeek",
    "openrouter": "OpenRouter",
    "claude": "Claude",
    "heuristico": "heuristico",
}


def avaliar_com_ia(dados: dict) -> dict:
    """Decide o score final combinando heuristica + Camada 1 + (talvez) Camada 2.

    Devolve o dicionario 'analise_ia' que o alerts.py sabe mostrar:
      {
        "score_final": int,
        "fonte_score": "heuristico" | "Groq" | "DeepSeek" | "OpenRouter" | "Claude",
        "camada1": {"score","justificacao"} | None,
        "camada2": {"score","justificacao"} | None,
      }
    """
    resultado = _resultado_heuristico(dados)

    # ---------- CAMADA 1 (Groq/DeepSeek/OpenRouter) - corre para TODOS ----------
    if ai_layer1.esta_configurada():
        try:
            c1 = ai_layer1.analisar_token(dados)
            resultado["camada1"] = c1
            resultado["score_final"] = c1["score"]
            resultado["provider_ia"] = c1.get("provider", "deepseek")
            resultado["fonte_score"] = _NOMES_DISPLAY_PROVIDER.get(
                resultado["provider_ia"], resultado["provider_ia"])
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
            resultado["provider_ia"] = "claude"
        except RuntimeError as e:
            alerts.info(f"[yellow]Camada 2 falhou, mantenho Camada 1:[/yellow] {e}")

    return resultado


def _notificar_compra_telegram(mensagem: str) -> None:
    """Notifica o Telegram de uma compra bem-sucedida (qualquer um dos 4
    modos). No-op se o Telegram nao estiver configurado. Nunca levanta
    excecao - uma notificacao falhada nunca deve travar o bot."""
    try:
        telegram_alerts.enviar(f"✅ COMPRA: {mensagem}")
    except Exception:
        pass


def _notificar_venda_telegram(mensagem: str, lucro_estimado_usd: float | None = None) -> None:
    """Notifica o Telegram de uma venda bem-sucedida, com o lucro/prejuizo
    ESTIMADO (a partir da variacao de preco - nao e o valor exato do
    ledger, so serve para a notificacao ser informativa sem custar uma
    leitura extra do carteira.json). Se a venda representar uma fatia
    grande do saldo atual (TELEGRAM_ALERTA_SALDO_PCT), acrescenta um aviso
    destacado de 'mudanca significativa'. Nunca levanta excecao."""
    try:
        texto = f"💰 VENDA: {mensagem}"
        if lucro_estimado_usd is not None:
            emoji = "📈" if lucro_estimado_usd >= 0 else "📉"
            texto += f"\n{emoji} Lucro/prejuízo estimado: ${lucro_estimado_usd:+.2f}"
            try:
                import carteira
                saldo = carteira.saldo_disponivel()
                if saldo > 0 and (abs(lucro_estimado_usd) / saldo * 100) >= config.TELEGRAM_ALERTA_SALDO_PCT:
                    pct = abs(lucro_estimado_usd) / saldo * 100
                    texto += f"\n⚠️ Mudança significativa: {pct:.1f}% do saldo atual!"
            except Exception:
                pass
        telegram_alerts.enviar(texto)
    except Exception:
        pass


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


def _valor_trade_dinamico(chain: str = "solana",
                          teto_modo: float | None = None) -> float:
    """Valor da PROXIMA compra: TRADE_PCT_SALDO% do saldo livre atual,
    preso entre TRADE_MIN_USD e TRADE_MAX_USD, e nunca acima do teto do
    modo (o valor fixo antigo de cada modo, que passa a funcionar como
    teto de seguranca - os executores ja o impoem por dentro).

    Saldo usado: em DRY_RUN, o saldo virtual (carteira.json); em REAL, o
    saldo on-chain da wallet da chain respetiva. Se nao der para
    determinar o saldo, usa TRADE_MIN_USD - fail-safe deliberado: na
    duvida arrisca-se o MINIMO, nunca o maximo."""
    saldo = None
    try:
        if config.DRY_RUN:
            import carteira
            saldo = carteira.saldo_disponivel()
        elif chain == "bsc":
            import wallet_bsc
            import executor_bsc
            saldo = (wallet_bsc.obter_saldo_bnb() or 0) * executor_bsc._preco_bnb_usd()
        else:
            import wallet
            saldo = wallet.obter_saldo_sol() * obter_preco_sol_usd()
    except Exception:
        saldo = None

    if not saldo or saldo <= 0:
        valor = config.TRADE_MIN_USD
    else:
        valor = max(config.TRADE_MIN_USD,
                    min(config.TRADE_MAX_USD,
                        saldo * config.TRADE_PCT_SALDO / 100.0))
    if teto_modo is not None:
        valor = min(valor, teto_modo)
    return round(valor, 2)


def _registar_decisao_memoria(dados: dict, analise_ia: dict | None,
                              decisao: str, motivo_rejeicao: str | None,
                              modo: str = "normal") -> None:
    """Regista uma decisao de compra/rejeicao na memoria semanal
    (memoria/decisoes.jsonl). Puramente informativo: nunca levanta e
    nunca muda nenhuma decisao - se falhar, o bot segue igual."""
    try:
        import memoria
        memoria.registar_decisao({
            "token": dados.get("token_simbolo"),
            "mint": dados.get("token_mint"),
            "chain": dados.get("chain", "solana"),
            "modo": modo,
            "liquidez_usd": dados.get("liquidez_usd"),
            "idade_s": round((dados.get("idade_minutos") or 0) * 60),
            "holder_concentrado_pct": (
                dados.get("top_holder_pct")
                if dados.get("holders_disponivel") else None
            ),
            "score_ia": (analise_ia or {}).get("score_final"),
            "provider_ia": (analise_ia or {}).get("provider_ia", "heuristico"),
            "decisao": decisao,
            "motivo_rejeicao": motivo_rejeicao,
            # Atividade de trading recente (traders unicos + volume da
            # janela mais recente) - None quando a GeckoTerminal nao
            # trouxe essa janela para o pool. Guardado sempre (mesmo em
            # compras) para poder analisar depois se o filtro esta
            # calibrado bem (ver _filtro_atividade_recente).
            "traders_unicos": dados.get("compradores_unicos"),
            "volume_usd_recente": dados.get("volume_usd_recente"),
        })
    except Exception as e:
        alerts.info(f"[dim][memoria] falha ao registar decisao: {e}[/dim]")


# ============================================================================
# Filtros BARATOS (nao dependem da IA): liquidez, honeypot, taxas, holders,
# idade minima da curva. Cada um e a UNICA fonte de verdade da sua regra -
# usados tanto pelo pre-filtro (antes da IA, so para decidir se vale a pena
# gastar uma chamada) como pelos tentar_comprar_*/curva/bsc (depois da IA,
# para a rejeicao/registo a serio). Isto evita duplicar limiares em dois
# sitios que podiam desalinhar-se com o tempo.
# ============================================================================
def _filtro_atividade_recente(dados: dict) -> tuple[bool, str | None]:
    """Traders unicos + volume da janela mais recente (m5/m15, capturados
    em detector.py a partir de "transactions"/"volume_usd" da
    GeckoTerminal) - exige alguma atividade GENUINA, para nao comprar
    tokens que so passam nos outros filtros (liquidez/holders) mas nao
    tem ninguem realmente a negociar.

    FAIL-OPEN (nao rejeita) quando o dado nao esta disponivel: um pool
    sem esta janela (API ainda nao indexou, ou o pool veio do
    detector_websocket.py, que nunca consulta a GeckoTerminal) NAO e um
    sinal de perigo - so falta de informacao para ESTE filtro em
    particular, por isso deixa os outros filtros (liquidez, autoridades,
    holders) decidirem. Ver justificacao completa na mensagem que
    acompanha este commit."""
    compradores = dados.get("compradores_unicos")
    volume = dados.get("volume_usd_recente")

    if compradores is not None and compradores < config.TRADERS_UNICOS_MINIMO:
        return False, (f"traders unicos: {compradores} < minimo "
                       f"{config.TRADERS_UNICOS_MINIMO}")
    if volume is not None and volume < config.VOLUME_MINIMO_USD:
        return False, (f"volume ${volume:,.0f} < minimo "
                       f"${config.VOLUME_MINIMO_USD:,.0f}")
    return True, None


def _filtro_barato_normal(dados: dict) -> tuple[bool, str | None]:
    """Piso de liquidez do caminho normal (Jupiter) + atividade real."""
    liquidez = dados.get("liquidez_usd") or 0.0
    if liquidez < config.LIQUIDEZ_MINIMA_USD:
        return False, (f"liquidez ${liquidez:,.0f} < minima "
                       f"${config.LIQUIDEZ_MINIMA_USD:,.0f}")
    return _filtro_atividade_recente(dados)


def _filtro_barato_curva(dados: dict) -> tuple[bool, str | None]:
    """Atraso minimo desde o lancamento (bonding curve) + atividade real."""
    idade_seg = dados.get("idade_minutos", 0) * 60
    if idade_seg < config.PUMPFUN_ATRASO_MINIMO_SEGUNDOS:
        return False, (f"idade {idade_seg:.0f}s < atraso minimo da curva "
                       f"{config.PUMPFUN_ATRASO_MINIMO_SEGUNDOS}s")
    return _filtro_atividade_recente(dados)


def _filtro_barato_bsc(dados: dict) -> tuple[bool, str | None]:
    """Liquidez BSC + anti-honeypot + taxas + holders (caminho BSC).
    NAO inclui a rota de venda (essa precisa de 3 eth_call - fica so no
    tentar_comprar_bsc, depois da IA, para nao gastar RPC em candidatos
    que a IA ainda pode rejeitar por outras razoes)."""
    simbolo = dados.get("token_simbolo", "?")
    liquidez = dados.get("liquidez_usd") or 0.0
    if liquidez < config.LIQUIDEZ_MINIMA_BSC_USD:
        return False, f"liquidez ${liquidez:,.0f} < ${config.LIQUIDEZ_MINIMA_BSC_USD:,.0f} BSC"

    if dados.get("honeypot") is True:
        return False, "honeypot detectado (simulacao diz que nao deixa vender)"
    if config.BSC_EXIGIR_ANTI_HONEYPOT:
        if dados.get("analise_indisponivel"):
            return False, "anti-honeypot sem resposta (token nao indexado/API em baixo) - fail-closed"
        sell_tax = dados.get("sell_tax")
        buy_tax = dados.get("buy_tax")
        if sell_tax is None:
            return False, "taxa de venda desconhecida (simulacao incompleta) - fail-closed"
        if sell_tax > config.BSC_SELL_TAX_MAX_PCT:
            return False, f"taxa de venda {sell_tax:.1f}% > {config.BSC_SELL_TAX_MAX_PCT:.0f}%"
        if buy_tax is not None and buy_tax > config.BSC_BUY_TAX_MAX_PCT:
            return False, f"taxa de compra {buy_tax:.1f}% > {config.BSC_BUY_TAX_MAX_PCT:.0f}%"

    holders_total = dados.get("holders_total")
    holders_falharam = dados.get("holders_falharam")
    if (holders_total and holders_total >= 20 and holders_falharam is not None
            and (holders_falharam / holders_total * 100) > config.BSC_HOLDERS_FALHA_MAX_PCT):
        return False, (f"{holders_falharam}/{holders_total} holders nao conseguem vender "
                       f"(> {config.BSC_HOLDERS_FALHA_MAX_PCT:.0f}%)")

    # Concentracao do maior holder - placeholder ate existir fonte de
    # dados na BSC (holders_disponivel=False). Avisa sempre que e saltado.
    if dados.get("holders_disponivel"):
        top_pct = dados.get("top_holder_pct") or 0.0
        if top_pct > config.BSC_TOP_HOLDER_MAX_PCT:
            return False, f"holder concentrado {top_pct:.0f}% > {config.BSC_TOP_HOLDER_MAX_PCT:.0f}%"
    else:
        alerts.info(f"[dim][BSC] {simbolo}: filtro de holder concentrado IGNORADO - "
                    f"sem fonte de dados de holders na BSC (ver BSC_TOP_HOLDER_MAX_PCT no config.py)[/dim]")

    return True, None


def _passa_filtros_baratos(dados: dict) -> tuple[bool, str | None]:
    """Decide, ANTES de chamar avaliar_com_ia(), se vale a pena gastar
    uma chamada de IA (Groq/DeepSeek/OpenRouter) neste token. Corre os
    MESMOS filtros que tentar_comprar_bsc/curva/normal aplicam depois -
    se um destes ja reprova, a IA nunca decidiria diferente, so custaria
    tokens/latencia/quota de rate-limit a toa (diagnostico real: >=10%
    das chamadas de IA desperdicadas em candidatos assim, sobretudo
    anti-honeypot sem resposta na BSC).

    So decide SE vale a pena gastar IA - a rejeicao e o registo na
    memoria semanal continuam a acontecer, como sempre, dentro de
    tentar_comprar_bsc/curva/normal (usando os MESMOS filtros, por isso
    o resultado nunca diverge: e so uma questao de QUANDO se descobre).

    Nao mexe em trading desligado (Fase 2/BSC off) - nesse caso a IA
    continua a correr para todos os tokens, como hoje, porque o modo
    "so deteccao + analise + alerta" (sem wallet) depende disso."""
    if not config.fase2_configurada():
        return True, None
    if dados.get("chain") == "bsc":
        if not config.fase2_bsc_configurada():
            return True, None
        return _filtro_barato_bsc(dados)
    if config.PUMPFUN_BONDING_CURVE_ATIVO and _e_pumpfun_curva(dados):
        return _filtro_barato_curva(dados)
    return _filtro_barato_normal(dados)


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
        _registar_decisao_memoria(dados, analise_ia, "rejeitado",
                                  f"score IA {score} > {config.PUMPFUN_SCORE_COMPRA_MAX} (curva)",
                                  modo="bonding_curve")
        return True  # era candidato de curva, mas rejeitado: NAO cai no fluxo normal

    # Atraso minimo (helper partilhado com o pre-filtro - ver _filtro_barato_curva)
    ok_idade, motivo_idade = _filtro_barato_curva(dados)
    if not ok_idade:
        alerts.info(f"[dim][curva] {simbolo} {motivo_idade} - espera[/dim]")
        _registar_decisao_memoria(dados, analise_ia, "rejeitado", motivo_idade,
                                  modo="bonding_curve")
        return True

    mint = dados["token_mint"]
    if mint in posicoes.listar_posicoes_abertas():
        return True

    # Dimensionamento dinamico: % do saldo livre, dentro dos limites
    valor = _valor_trade_dinamico(teto_modo=config.PUMPFUN_MAX_TRADE_USD)

    if config.DRY_RUN:
        import carteira
        if carteira.saldo_disponivel() < valor:
            return True

    try:
        import executor_pumpfun
        preco_sol_usd = obter_preco_sol_usd()
        r = executor_pumpfun.comprar_na_curva(
            mint=mint, simbolo=simbolo,
            valor_usd=valor, preco_sol_usd=preco_sol_usd,
            liquidez_usd=dados.get("liquidez_usd"),
            idade_minutos_compra=dados.get("idade_minutos"),
            top_holder_pct=dados.get("top_holder_pct"),
            holders_disponivel=dados.get("holders_disponivel"),
        )
        cor = "green" if r.get("sucesso") else "yellow"
        alerts.info(f"[{cor}]{r['mensagem']}[/{cor}]")
        if r.get("sucesso"):
            _notificar_compra_telegram(r["mensagem"])
            _registar_decisao_memoria(dados, analise_ia, "comprado", None,
                                      modo="bonding_curve")
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
        motivo = dados.get("onchain_motivo_indisponivel")
        detalhe = _MOTIVOS_ONCHAIN_INDISPONIVEL.get(motivo, "motivo nao registado")
        return False, f"dados on-chain indisponiveis ({detalhe})"
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

    POLITICA DE DADOS EM FALTA - MUDADA apos diagnostico (ver resumo desta
    sessao): antes, se a chamada RPC falhasse, os sinais de momentum eram
    SALTADOS (fail-open, nao bloqueava). Na pratica, num RPC publico (o
    default do bot), estas chamadas falham/rate-limitam com frequencia -
    o que significava que a maioria das compras do Caveira pagava os 5s+
    de atraso da janela de momentum SEM ganhar nenhuma seletividade real
    (o filtro fazia fail-open quase sempre). Resultado observado: pior
    timing de entrada, sem melhor selecao - exatamente o padrao dos
    numeros reportados (win rate a piorar apos este filtro entrar).
    Agora e FAIL-CLOSED: se nao conseguirmos confirmar a atividade real
    (RPC falhou), REJEITAMOS o token - o Caveira so compra quando tem a
    certeza da qualidade, nao quando simplesmente nao conseguiu verificar.
    AVISO IMPORTANTE: isto significa que num RPC publico o Caveira pode
    passar a comprar MUITO menos (ou quase nada) - e o preco de ser
    realmente seletivo. Se quiseres que o Caveira dispare com regularidade,
    precisas de um RPC melhor (Helius/QuickNode) para estas chamadas
    terem sucesso com frequencia suficiente.
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
        return False, ("dados de momentum INDISPONIVEIS (RPC falhou/rate-limitou) - "
                       "FAIL-CLOSED: sem confirmar atividade real, nao arrisca a compra")

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


# Fila de retentativa do Caveira: mint -> {"dados": dict, "detectado_em": float}.
# So em memoria (perde-se num restart - aceitavel, e so uma janela de
# poucos segundos). Ver tentar_comprar_sniper_rapido() e
# _reprocessar_caveira_pendentes().
#
# DIAGNOSTICO REAL (token CWC, 2026-07): uma retentativa agendada para
# ~40s so foi processada ~10 MINUTOS depois. Causa confirmada por leitura
# do codigo: _reprocessar_caveira_pendentes() so corria 1x por volta do
# ciclo principal (main.py, dentro do "while True"), e o ciclo NAO tem
# duracao fixa - com POLL_INTERVAL_SEGUNDOS a regular so a PAUSA entre
# ciclos, nao o que acontece DENTRO de um (ate MAX_ANALISES_POR_CICLO
# tokens, cada um com varias chamadas RPC com retries/backoff ate ~22.5s
# cada em caso de 429, mais o sleep de CAVEIRA_JANELA_MOMENTUM_SEGUNDOS
# por candidato do Caveira, mais verificar_posicoes()/reavaliar_watchlist()
# no fim). Um RPC publico sob carga facilmente estica isso a minutos.
#
# CORRIGIDO: a fila passou a ter uma thread dedicada
# (_loop_retentativas_caveira, arrancada em main()) que a revisita a cada
# _INTERVALO_RETENTATIVA_CAVEIRA_SEGUNDOS segundos, SEMPRE - independente
# de quanto o ciclo principal demorar. O lock protege o dict (agora
# escrito pela thread principal e lido/esvaziado pela thread de
# retentativas); ver tambem os locks novos em posicoes.py/carteira.py/
# sniper_rapido.py - com uma 2a thread capaz de comprar a serio, essas
# escritas deixaram de ser seguras sem eles.
_caveira_pendentes: dict[str, dict] = {}
_lock_caveira_pendentes = threading.Lock()

# Cadencia da thread de retentativas - independente de POLL_INTERVAL_
# SEGUNDOS/METODO_DETECCAO de proposito (essa e a falha que causou o
# atraso de 10 minutos: a retentativa dependia da cadencia do ciclo
# principal). 2s cobre CAVEIRA_ATRASO_MINIMO_SEGUNDOS (default 6s) em
# poucas voltas, sem gastar CPU a verificar uma fila tipicamente vazia.
_INTERVALO_RETENTATIVA_CAVEIRA_SEGUNDOS = 2.0


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

    # RETENTATIVA (uma so vez por mint): o WebSocket deteta tokens em
    # 0-2s de vida - tempo insuficiente para o RPC ter indexado a conta
    # do mint, o que faz onchain_disponivel vir False so por timing, nao
    # por o token ter algum problema real. Em vez de rejeitar logo,
    # agenda-se UMA retentativa depois de CAVEIRA_ATRASO_MINIMO_SEGUNDOS -
    # processada pela thread dedicada _loop_retentativas_caveira (nao
    # pelo ciclo principal - ver o aviso junto de _caveira_pendentes mais
    # acima). O marcador "_caveira_retentativa" evita reagendar outra vez
    # se a 1a retentativa ainda vier sem dados (nesse caso ja e um "nao
    # consigo mesmo", nao um problema de timing - reprova como sempre).
    idade_seg = dados.get("idade_minutos", 0) * 60
    if (not dados.get("onchain_disponivel")
            and idade_seg < config.CAVEIRA_ATRASO_MINIMO_SEGUNDOS
            and not dados.get("_caveira_retentativa")):
        with _lock_caveira_pendentes:
            ja_agendado = mint in _caveira_pendentes
            if not ja_agendado:
                _caveira_pendentes[mint] = {"dados": dados, "detectado_em": time.monotonic()}
        if not ja_agendado:
            alerts.info(
                f"[dim][💀 sniper] {simbolo} ainda sem dados on-chain aos "
                f"{idade_seg:.1f}s de vida - agendada 1 retentativa em "
                f"~{config.CAVEIRA_ATRASO_MINIMO_SEGUNDOS:.0f}s (RPC pode nao ter "
                f"indexado a conta ainda)[/dim]"
            )
            return False

    # Checklist binaria (substitui o score heuristico neste modo)
    passou, motivo = _passa_checklist_caveira(dados)
    if not passou:
        alerts.info(f"[dim][💀 sniper] {simbolo} reprovado na checklist: {motivo}[/dim]")
        _registar_decisao_memoria(dados, None, "rejeitado",
                                  f"checklist caveira: {motivo}",
                                  modo="sniper_rapido")
        return False

    if mint in posicoes.listar_posicoes_abertas():
        return False  # ja ha posicao neste token (de qualquer modo)

    # Dimensionamento dinamico: % do saldo livre, dentro dos limites
    # (SNIPER_RAPIDO_VALOR_USD passa a ser o TETO deste modo - por
    # defeito $1, o que na pratica mantem o sniper na compra minuscula
    # de sempre; sobe-o no .env se quiseres que acompanhe o saldo)
    valor = _valor_trade_dinamico(teto_modo=config.SNIPER_RAPIDO_VALOR_USD)

    # Limite diario OBRIGATORIO - a principal trava deste modo (verificado
    # ANTES do filtro de qualidade, que e mais lento - nao vale a pena
    # esperar a janela de momentum so para descobrir que o limite ja bateu)
    import sniper_rapido
    if not sniper_rapido.pode_gastar(valor):
        alerts.info(
            f"[dim][💀 sniper] {simbolo} ignorado - limite diario atingido "
            f"(restam ${sniper_rapido.restante_hoje_usd():.2f})[/dim]"
        )
        _registar_decisao_memoria(dados, None, "rejeitado",
                                  "limite diario do caveira atingido",
                                  modo="sniper_rapido")
        return False

    # Filtro de QUALIDADE (momentum) - o mais lento dos checks deste modo
    # (pode esperar alguns segundos + chamadas RPC extra), por isso corre
    # por ultimo, so depois de todos os checks baratos terem passado.
    passou_qualidade, motivo_qualidade = _passa_filtro_qualidade_caveira(dados)
    if not passou_qualidade:
        alerts.info(f"[dim][💀 sniper] {simbolo} reprovado no filtro de qualidade: {motivo_qualidade}[/dim]")
        _registar_decisao_memoria(dados, None, "rejeitado",
                                  f"filtro de qualidade caveira: {motivo_qualidade}",
                                  modo="sniper_rapido")
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
                                   pool_address=dados.get("pool_address"),
                                   liquidez_usd=dados.get("liquidez_usd"),
                                   idade_minutos_compra=dados.get("idade_minutos"),
                                   top_holder_pct=dados.get("top_holder_pct"),
                                   holders_disponivel=dados.get("holders_disponivel"))
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
            _notificar_compra_telegram(f"💀 [SNIPER] {r['mensagem']}")
            _registar_decisao_memoria(dados, None, "comprado", None,
                                      modo="sniper_rapido")
            return True
        alerts.info(f"[yellow]💀 [sniper] falha ao comprar {simbolo}: {r.get('mensagem')}[/yellow]")
    except Exception as e:
        alerts.info(f"[red]💀 [sniper] erro a comprar {simbolo}:[/red] {e}")
    return False


# Traduz o motivo estrutural de indisponibilidade dos dados on-chain
# (ver _buscar_mint_info_com_motivo) numa frase legivel no log/memoria.
# Existe SO para a pessoa a ler o log ter a CERTEZA de qual foi (rate
# limit do RPC vs conta confirmada ausente), em vez de teres de adivinhar
# como aconteceu com o CWC - ver o diagnostico junto de _caveira_pendentes.
_MOTIVOS_ONCHAIN_INDISPONIVEL = {
    "rate_limit": "RPC rate-limitado (429 persistente) - NAO e evidencia de "
                  "problema no token, e do RPC publico sob carga",
    "conta_ausente": "conta do mint CONFIRMADA ausente no RPC (resposta "
                      "valida, sem erro) - a esta idade, ja nao e um "
                      "problema de indexacao lenta",
    "erro_rpc": "erro RPC nao classificado (rede/timeout/resposta invalida)",
}


def _buscar_mint_info_com_motivo(mint: str) -> tuple[dict | None, str | None]:
    """Chama rpc.get_mint_info(mint) e devolve (info, motivo). 'motivo' e
    None quando info nao e None (sucesso); caso contrario e uma das
    chaves de _MOTIVOS_ONCHAIN_INDISPONIVEL - a distincao que faltava
    entre "RPC recusou-se a responder" e "conta confirmada ausente" (ver
    o diagnostico do CWC junto de _caveira_pendentes). Antes, os dois
    colapsavam no mesmo None e na mesma mensagem generica "dados on-chain
    indisponiveis", tornando impossivel saber qual dos dois aconteceu SO
    pelo log."""
    try:
        info = rpc.get_mint_info(mint)
    except rpc.RPCRateLimit:
        return None, "rate_limit"
    except rpc.RPCError:
        return None, "erro_rpc"
    if info is None:
        return None, "conta_ausente"
    return info, None


def _reprocessar_caveira_pendentes() -> None:
    """Processa a fila de retentativa do Caveira (ver _caveira_pendentes
    em tentar_comprar_sniper_rapido). Chamada pela thread dedicada
    _loop_retentativas_caveira a cada _INTERVALO_RETENTATIVA_CAVEIRA_
    SEGUNDOS, SEMPRE - ja nao depende da duracao do ciclo principal (ver
    o diagnostico do CWC no comentario junto de _caveira_pendentes).

    Para cada mint cujo tempo de espera ja passou: busca os dados on-chain
    (mint/freeze authority) DE NOVO - por essa altura o RPC ja deve ter
    indexado a conta - e tenta a compra outra vez, com o marcador
    "_caveira_retentativa" para garantir UMA SO retentativa por mint
    (se ainda vier sem dados, e reprovado a serio, como sempre foi - mas
    agora com o MOTIVO especifico registado, nao so "indisponivel").

    Nunca levanta - uma falha aqui nao pode derrubar a thread de
    retentativas nem o ciclo principal."""
    if not _caveira_pendentes:
        return
    if not config.SNIPER_RAPIDO_ATIVO:
        # Modo desligado entretanto - a fila perdeu o sentido
        with _lock_caveira_pendentes:
            _caveira_pendentes.clear()
        return

    with _lock_caveira_pendentes:
        prontos = [
            mint for mint, info in _caveira_pendentes.items()
            if time.monotonic() - info["detectado_em"] >= config.CAVEIRA_ATRASO_MINIMO_SEGUNDOS
        ]
        infos_prontos = {mint: _caveira_pendentes.pop(mint) for mint in prontos}

    for mint, info in infos_prontos.items():
        dados = info["dados"]
        try:
            # Ja foi comprado por outro caminho entretanto (normal/curva/
            # manual) enquanto esperava - nao faz sentido tentar de novo
            if mint in posicoes.listar_posicoes_abertas():
                continue

            # Idade real (nao a que tinha no momento da deteccao) - para
            # o teto IDADE_MAXIMA_CAVEIRA_SEGUNDOS da checklist ser fiel
            espera_seg = time.monotonic() - info["detectado_em"]
            dados["idade_minutos"] = dados.get("idade_minutos", 0) + espera_seg / 60

            # Busca fresca do mint - so isto, nao a analise on-chain
            # completa (holders/liquidez bloqueada/deployer): e so isto
            # que a checklist do Caveira precisa, e mantem a retentativa
            # barata (1 RPC, ja passa pelo limitador global em rpc.py)
            info_mint, motivo_indisponivel = _buscar_mint_info_com_motivo(mint)
            if info_mint is not None:
                dados["onchain_disponivel"] = True
                dados["mint_authority"] = info_mint["mint_authority"]
                dados["freeze_authority"] = info_mint["freeze_authority"]
                dados["onchain_motivo_indisponivel"] = None
            else:
                dados["onchain_motivo_indisponivel"] = motivo_indisponivel
                alerts.info(
                    f"[dim][💀 sniper] retentativa de {dados.get('token_simbolo', mint[:8])} "
                    f"ainda sem dados on-chain apos {espera_seg:.0f}s de espera - "
                    f"{_MOTIVOS_ONCHAIN_INDISPONIVEL.get(motivo_indisponivel, motivo_indisponivel)}[/dim]"
                )

            dados["_caveira_retentativa"] = True  # nunca reagenda 2a vez
            comprou = tentar_comprar_sniper_rapido(dados)

            # Mesma logica que o processar_pool aplica a seguir a uma
            # compra normal do Caveira: a IA (mais lenta) avalia so agora
            # e pode mandar vender de urgencia - sem isto, compras vindas
            # da retentativa nunca teriam essa 2a camada de protecao.
            if comprou:
                analise_ia = avaliar_com_ia(dados)
                vender_sniper_se_score_mau(dados, analise_ia)
        except Exception as e:
            alerts.info(f"[red][💀 sniper] falha ao reprocessar retentativa de "
                        f"{dados.get('token_simbolo', mint[:8])}:[/red] {e}")


def _loop_retentativas_caveira() -> None:
    """Thread dedicada as retentativas do Caveira - DESACOPLADA do ciclo
    principal de proposito (ver o diagnostico do CWC no comentario junto
    de _caveira_pendentes: antes, a fila so era revisitada 1x por volta
    do "while True" em main(), e essa volta podia demorar minutos com
    varios tokens a analisar). Corre para sempre, verificando a fila a
    cada _INTERVALO_RETENTATIVA_CAVEIRA_SEGUNDOS, SEMPRE - mesmo que o
    ciclo principal esteja preso numa chamada RPC lenta.

    Ao contrario do Copy Trading (que so RECOLHE sinais numa thread e
    deixa o ciclo principal executar a compra a serio), esta thread FAZ a
    compra a serio quando a retentativa passa - por isso os ficheiros que
    toca (posicoes.json/carteira.json/sniper_rapido.json) precisaram de
    um lock interno (ver posicoes.py/carteira.py/sniper_rapido.py): e a
    1a vez que mais que uma thread pode escrever neles ao mesmo tempo.

    Daemon (arrancada com daemon=True em main()) - nao impede o processo
    de terminar no Ctrl+C/SIGTERM, tal como o WebSocket e o Copy Trading."""
    while True:
        time.sleep(_INTERVALO_RETENTATIVA_CAVEIRA_SEGUNDOS)
        try:
            _reprocessar_caveira_pendentes()
        except Exception as e:
            alerts.info(f"[red]Erro na thread de retentativas do Caveira:[/red] {e}")


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
        r = executor.vender_token(mint, 100, motivo_venda="venda_urgente_ia")
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
            _notificar_compra_telegram(f"👥 [COPY de {carteira_seguida[:8]}...] {r['mensagem']}")
            # Memoria semanal: o copy trade nao tem 'dados' de analise (nao
            # passa pelo pipeline de pools) - regista com o minimo que sabe,
            # para TODA a compra real ficar em decisoes.jsonl
            _registar_decisao_memoria(
                {"token_simbolo": simbolo, "token_mint": mint, "chain": "solana"},
                None, "comprado", None, modo="copy_trading")
            return
        alerts.info(f"[yellow][COPY] falha ao comprar {mint[:8]}...: {r.get('mensagem')}[/yellow]")
    except Exception as e:
        alerts.info(f"[red][COPY] erro a comprar {mint[:8]}...:[/red] {e}")


def tentar_comprar_bsc(dados: dict, analise_ia: dict) -> None:
    """Compra na BSC (via PancakeSwap), o equivalente ao tentar_comprar da
    Solana. Precisa da wallet BSC configurada; usa o limite BSC_MAX_TRADE_USD.
    Nunca deixa uma falha derrubar o bot.

    FILTROS ENDURECIDOS (pos-diagnostico do prejuizo BSC): alem do score
    da IA, a compra agora exige liquidez minima PROPRIA da BSC, confirmacao
    anti-honeypot FAIL-CLOSED (a Honeypot.is simula compra+venda num fork
    da chain - se nao confirmar que o token deixa vender, nao compra),
    taxas de compra/venda dentro do teto, holders sem falhas de venda em
    massa e rota de venda viva na PancakeSwap. Cada rejeicao fica no log
    E na memoria semanal com o motivo especifico, para calibrar limiares."""
    if not config.fase2_bsc_configurada():
        return  # sem WALLET_PRIVATE_KEY_BSC, trading BSC desligado

    mint = dados["token_mint"]
    simbolo = dados["token_simbolo"]

    def _rejeitar(motivo: str) -> None:
        alerts.info(f"[dim][BSC] {simbolo} rejeitado: {motivo}[/dim]")
        _registar_decisao_memoria(dados, analise_ia, "rejeitado", motivo)

    score = analise_ia["score_final"]
    if score > config.SCORE_COMPRA_MAX:
        _rejeitar(f"score IA {score} > {config.SCORE_COMPRA_MAX}")
        return

    # Filtros 1, 3, 4a, 4b, 5 (helper partilhado com o pre-filtro - ver
    # _filtro_barato_bsc: liquidez, anti-honeypot, taxas, holders)
    ok_barato, motivo_barato = _filtro_barato_bsc(dados)
    if not ok_barato:
        _rejeitar(motivo_barato)
        return

    if mint in posicoes.listar_posicoes_abertas():
        return

    # Dimensionamento dinamico: % do saldo livre, dentro dos limites
    valor = _valor_trade_dinamico("bsc", teto_modo=config.BSC_MAX_TRADE_USD)

    if config.DRY_RUN:
        import carteira
        if carteira.saldo_disponivel() < valor:
            return

    try:
        import executor_bsc

        # Filtro 2: rota de venda viva na PancakeSwap ANTES de comprar
        # (o check mais caro - 3 eth_calls - por isso corre em ultimo)
        ok_rota, motivo_rota = executor_bsc.verificar_rota_venda(mint, valor)
        if not ok_rota:
            _rejeitar(f"rota de venda: {motivo_rota}")
            return

        r = executor_bsc.comprar_token(mint, simbolo, valor_usd=valor,
                                       dex=dados.get("dex"),
                                       liquidez_usd=dados.get("liquidez_usd"),
                                       idade_minutos_compra=dados.get("idade_minutos"))
        cor = "green" if r.get("sucesso") else "yellow"
        alerts.info(f"[{cor}]{r['mensagem']}[/{cor}]")
        if r.get("sucesso"):
            _notificar_compra_telegram(r["mensagem"])
            _registar_decisao_memoria(dados, analise_ia, "comprado", None)
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
        _registar_decisao_memoria(dados, analise_ia, "rejeitado",
                                  f"score IA {score} > {config.SCORE_COMPRA_MAX}")
        return  # risco demasiado alto, nao compra

    # Piso de liquidez no caminho NORMAL (helper partilhado com o
    # pre-filtro - ver _filtro_barato_normal). Sem isto, o BONUS de "LP
    # bloqueado" do pump.fun (-20) cancelava a penalizacao de liquidez
    # baixa (+20), dando score 0 e comprando tokens com liquidez
    # $0/desconhecida (observado ao vivo). Tokens pump.fun recem-nascidos
    # sao para o modo bonding curve (que le a curva on-chain), nao para
    # este caminho.
    ok_liquidez, motivo_liquidez = _filtro_barato_normal(dados)
    if not ok_liquidez:
        alerts.info(f"[dim]{dados['token_simbolo']}: {motivo_liquidez} - "
                    f"nao compra no caminho normal[/dim]")
        _registar_decisao_memoria(dados, analise_ia, "rejeitado", motivo_liquidez)
        return

    mint = dados["token_mint"]
    simbolo = dados["token_simbolo"]

    # Nao compra o mesmo token duas vezes
    if mint in posicoes.listar_posicoes_abertas():
        return

    # Dimensionamento dinamico: % do saldo livre, dentro dos limites
    valor = _valor_trade_dinamico(teto_modo=config.MAX_TRADE_USD)

    # Em dry-run, respeita o saldo virtual disponivel
    if config.DRY_RUN:
        import carteira
        if carteira.saldo_disponivel() < valor:
            alerts.info(
                f"[yellow]Saldo virtual insuficiente para comprar {simbolo} "
                f"(disponivel: ${carteira.saldo_disponivel():.2f})[/yellow]"
            )
            return

    try:
        preco_sol_usd = obter_preco_sol_usd()
        resultado = executor.comprar_token(
            mint=mint, simbolo=simbolo,
            valor_usd=valor, preco_sol_usd=preco_sol_usd,
            decimais=dados.get("decimais"),
            dex=dados.get("dex"), modo="normal",
            pool_address=dados.get("pool_address"),
            liquidez_usd=dados.get("liquidez_usd"),
            idade_minutos_compra=dados.get("idade_minutos"),
            top_holder_pct=dados.get("top_holder_pct"),
            holders_disponivel=dados.get("holders_disponivel"),
        )
        etiqueta = "[SIMULADO]" if resultado["dry_run"] else "[REAL]"
        alerts.info(f"[green]{etiqueta} COMPRA: {resultado['mensagem']}[/green]")
        if resultado.get("sucesso"):
            _notificar_compra_telegram(resultado["mensagem"])
            _registar_decisao_memoria(dados, analise_ia, "comprado", None)
    except Exception as e:
        alerts.info(f"[red]Falha na compra de {simbolo}:[/red] {e}")


def _vender_posicao(mint: str, chain: str, percentagem: float,
                    motivo: str | None = None) -> dict:
    """Vende uma percentagem de uma posicao, escolhendo o executor certo
    pela chain (PancakeSwap para BSC, Jupiter para Solana). 'motivo' segue
    para a memoria semanal (trades_fechados.jsonl) - nao muda a venda."""
    if chain == "bsc":
        import executor_bsc
        return executor_bsc.vender_token(mint, percentagem, motivo_venda=motivo)
    return executor.vender_token(mint, percentagem, motivo_venda=motivo)


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
                # <= 0 tambem e tratado como "sem dados" - NUNCA decidir uma
                # venda (stop-loss/tempo/trailing) com base numa cotacao
                # invalida ou zerada; salta a posicao neste ciclo e tenta
                # outra vez no proximo
                if valor_atual_usd is None or valor_atual_usd <= 0:
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
                # Mesma protecao do lado Solana: cotacao a 0 nao e um preco,
                # e ausencia de dados - nao pode disparar stop-loss a -100%
                if valor_atual_usd <= 0:
                    raise RuntimeError("cotacao Jupiter devolveu 0")
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
                r = _vender_posicao(mint, chain, 100, motivo="stop_loss")
                alerts.info(f"[red]{r['mensagem']}[/red]")
                if r.get("sucesso"):
                    lucro_est = pos["valor_investido_usd"] * (variacao_pct / 100)
                    _notificar_venda_telegram(f"🔴 STOP-LOSS: {r['mensagem']}", lucro_est)
            except Exception as e:
                alerts.info(f"[red]Falha ao vender {pos['simbolo']}:[/red] {e}")
            continue

        # --- VENDA POR TEMPO SEM VALORIZACAO ---
        # Independente do stop-loss/take-profit: nao deixa dinheiro parado
        # indefinidamente num token que nao esta a ir a lado nenhum. So
        # dispara se o preco NAO estiver acima do preco de compra (sem
        # lucro nenhum, nem pequeno) - se ja houver lucro, mesmo pequeno,
        # o take-profit/trailing normais e que decidem. Aplica-se a
        # TODOS os modos de compra (nao ha isolamento aqui de proposito).
        timestamp_compra = pos.get("timestamp_compra")
        if timestamp_compra and variacao_pct <= 0:
            try:
                aberta_desde = datetime.fromisoformat(timestamp_compra)
                horas_aberta = (datetime.now(timezone.utc) - aberta_desde).total_seconds() / 3600
            except (TypeError, ValueError):
                horas_aberta = 0
            if horas_aberta >= config.TEMPO_MAXIMO_SEM_LUCRO_HORAS:
                alerts.info(
                    f"[yellow]{pos['simbolo']}: aberta ha {horas_aberta:.1f}h sem lucro "
                    f"({variacao_pct:+.1f}%) - a vender por TEMPO (>= "
                    f"{config.TEMPO_MAXIMO_SEM_LUCRO_HORAS}h sem valorizacao)...[/yellow]"
                )
                try:
                    r = _vender_posicao(mint, chain, 100, motivo="tempo_sem_lucro")
                    alerts.info(f"[yellow]{r['mensagem']}[/yellow]")
                    if r.get("sucesso"):
                        lucro_est = pos["valor_investido_usd"] * (variacao_pct / 100)
                        _notificar_venda_telegram(f"⏳ VENDA POR TEMPO ({horas_aberta:.1f}h sem lucro): {r['mensagem']}", lucro_est)
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
                        r = _vender_posicao(mint, chain, 100, motivo="reversao_volume")
                        alerts.info(f"[yellow]{r['mensagem']}[/yellow]")
                        if r.get("sucesso"):
                            lucro_est = pos["valor_investido_usd"] * (variacao_pct / 100)
                            _notificar_venda_telegram(f"🔃 REVERSÃO DE VOLUME: {r['mensagem']}", lucro_est)
                    except Exception as e:
                        alerts.info(f"[red]Falha ao vender {pos['simbolo']}:[/red] {e}")
                    continue

        # --- MODO DE SAIDA: "take_profit_parcial" (default) ou "trailing_puro" ---
        # O STOP_LOSS_PCT normal (desde o preco de COMPRA, ja tratado acima)
        # continua ativo em AMBOS os modos, como rede de seguranca para
        # quando o preco nunca chega a subir. A diferenca esta so em COMO
        # se sai de uma posicao que ESTA a subir.
        if config.MODO_SAIDA == "trailing_puro":
            # --- TRAILING PURO: nunca vende parcialmente. So vende 100%
            # quando o preco cai TRAILING_PURO_PCT% do pico mais alto ja
            # atingido. Ignora TAKE_PROFIT_MULTIPLICADOR/VENDER_PCT. ---
            if pico > 0:
                queda_desde_pico_pct = (pico - preco_atual) / pico * 100
                if queda_desde_pico_pct >= config.TRAILING_PURO_PCT:
                    alerts.info(
                        f"[yellow]TRAILING PURO disparado em {pos['simbolo']} "
                        f"(caiu {queda_desde_pico_pct:.1f}% do pico de ${pico:.6g}). "
                        f"A vender tudo...[/yellow]"
                    )
                    try:
                        r = _vender_posicao(mint, chain, 100, motivo="trailing_puro")
                        alerts.info(f"[yellow]{r['mensagem']}[/yellow]")
                        if r.get("sucesso"):
                            lucro_est = pos["valor_investido_usd"] * (variacao_pct / 100)
                            _notificar_venda_telegram(f"🟡 TRAILING PURO: {r['mensagem']}", lucro_est)
                    except Exception as e:
                        alerts.info(f"[red]Falha ao vender {pos['simbolo']}:[/red] {e}")
        else:
            # --- TAKE-PROFIT PARCIAL (comportamento original, default) ---
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
                    r = _vender_posicao(mint, chain, config.TAKE_PROFIT_VENDER_PCT,
                                    motivo="take_profit")
                    alerts.info(f"[green]{r['mensagem']}[/green]")
                    posicoes.atualizar_posicao(mint, take_profit_disparado=True)
                    if r.get("sucesso"):
                        lucro_est = pos["valor_investido_usd"] * (config.TAKE_PROFIT_VENDER_PCT / 100) * (variacao_pct / 100)
                        _notificar_venda_telegram(f"🟢 TAKE-PROFIT: {r['mensagem']}", lucro_est)
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
                        r = _vender_posicao(mint, chain, 100, motivo="trailing_stop")
                        alerts.info(f"[yellow]{r['mensagem']}[/yellow]")
                        if r.get("sucesso"):
                            lucro_est = pos["valor_investido_usd"] * (variacao_pct / 100)
                            _notificar_venda_telegram(f"🟡 TRAILING STOP: {r['mensagem']}", lucro_est)
                    except Exception as e:
                        alerts.info(f"[red]Falha ao vender {pos['simbolo']}:[/red] {e}")


def _registar_watchlist_memoria(r: dict, preco_usd, abertas: set) -> None:
    """Regista uma reavaliacao da watchlist na memoria semanal
    (memoria/watchlist_historico.jsonl). Nunca levanta. O preco segue a
    MESMA convencao da reavaliacao normal (BSC: valor de 1 token inteiro;
    Solana: preco por unidade minima); 'liquidez_usd' e o valor conhecido
    na DETECAO - este ciclo nao volta a cotar liquidez de proposito (so
    preco/rota), para nao duplicar chamadas as APIs."""
    try:
        import memoria
        tempo_min = None
        if r.get("adicionado_em"):
            try:
                adicionado = datetime.fromisoformat(r["adicionado_em"])
                tempo_min = round(
                    (datetime.now(timezone.utc) - adicionado).total_seconds() / 60, 1)
            except (TypeError, ValueError):
                pass
        memoria.registar_watchlist({
            "token": r.get("simbolo"),
            "mint": r.get("mint"),
            "chain": r.get("chain", "solana"),
            "preco_usd": preco_usd,
            "liquidez_usd": r.get("liquidez_usd"),
            "tempo_desde_deteccao_min": tempo_min,
            "em_posicao": r.get("mint") in abertas,
        })
    except Exception as e:
        alerts.info(f"[dim][memoria] falha ao registar watchlist: {e}[/dim]")


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

    # Posicoes abertas (uma leitura para o loop todo) - para a memoria
    # semanal saber se o token da watchlist tambem esta em carteira
    try:
        abertas = set(posicoes.listar_posicoes_abertas().keys())
    except Exception:
        abertas = set()

    for r in registos:
        mint = r.get("mint")
        if not mint:
            continue
        chain = r.get("chain", "solana")
        preco_registado = None  # o preco desta reavaliacao (None = sem rota)
        try:
            if chain == "bsc":
                # BSC: uma quantidade simbolica do token -> ha rota na PancakeSwap?
                import executor_bsc
                valor = executor_bsc.valor_atual_usd(mint, 10**18)  # 1 token (18 dec)
                if valor and valor > 0:
                    preco_registado = valor
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
                    preco_registado = preco_unitario
                    watchlist.atualizar_reavaliacao(mint, preco_unitario, liquidez_viva=True)
                else:
                    watchlist.atualizar_reavaliacao(mint, None, liquidez_viva=False)
        except Exception:
            # Sem rota de troca -> liquidez provavelmente morta/rugada
            watchlist.atualizar_reavaliacao(mint, None, liquidez_viva=False)
        _registar_watchlist_memoria(r, preco_registado, abertas)
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

    # Filtros baratos (liquidez, honeypot, taxas, holders, idade) ANTES da
    # IA: se ja sabemos que o token vai ser rejeitado por um destes, nao
    # vale a pena gastar uma chamada de IA (Groq/DeepSeek/OpenRouter) -
    # diagnostico real confirmou >=10% das chamadas desperdicadas assim.
    # A rejeicao "a serio" (com registo na memoria semanal) continua a
    # acontecer dentro de tentar_comprar - aqui so decidimos se poupamos
    # a chamada de IA; o resultado da compra nunca muda.
    pre_ok, pre_motivo = _passa_filtros_baratos(dados)
    if pre_ok:
        analise_ia = avaliar_com_ia(dados)
    else:
        analise_ia = _resultado_heuristico(dados)
        alerts.info(
            f"[dim]{dados['token_simbolo']}: reprovado num filtro barato "
            f"({pre_motivo}) - poupa a chamada de IA[/dim]"
        )
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
        # <= (nao <) no limite inferior: score == SCORE_COMPRA_MAX e um
        # valor MUITO comum na pratica (ex: score_heuristico = so
        # PESO_LIQUIDEZ_BAIXA quando onchain_disponivel/holders_disponivel
        # sao False - default 20, igual ao SCORE_COMPRA_MAX default) - a
        # condicao estrita excluia sistematicamente esses candidatos da
        # watchlist, mesmo sendo exatamente o caso "score no limiar" que
        # a watchlist existe para capturar.
        e_fronteira = (
            config.SCORE_COMPRA_MAX
            <= score
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

    # Retentativas do Caveira: thread dedicada, DESACOPLADA do ciclo
    # principal (ver o diagnostico do CWC no comentario junto de
    # _caveira_pendentes). So arranca com Fase 2 configurada - sem
    # wallet nao ha trading nenhum, o modo caveira nem sequer compra.
    if config.fase2_configurada():
        threading.Thread(
            target=_loop_retentativas_caveira, daemon=True
        ).start()
        alerts.console.print(
            f"[dim]Retentativas do Caveira: thread dedicada a cada "
            f"{_INTERVALO_RETENTATIVA_CAVEIRA_SEGUNDOS:.0f}s.[/dim]"
        )

    ciclo = 0
    ultima_verificacao_posicoes = 0.0

    while True:
        ciclo += 1

        # Junta os novos de TODAS as redes ativas neste ciclo, agrupados
        # por rede (mantendo a ordem de deteccao dentro de cada uma)
        novos_por_rede: dict[str, list] = {}
        for det in detetores:
            try:
                pools = det.buscar_novos()
                if pools:
                    novos_por_rede.setdefault(det.rede, []).extend(pools)
            except Exception as e:
                alerts.info(f"[red]Erro a detetar pools ({det.rede}):[/red] {e}")

        # Intercala (round-robin) entre redes para dar uma cota justa a
        # cada uma - sem isto, a Solana (que gera muito mais tokens por
        # ciclo) ocupava sozinha todas as vagas de MAX_ANALISES_POR_CICLO
        # e a BSC nunca chegava a ser processada, mesmo com REDES_ATIVAS
        # a incluir as duas. Se uma rede esgotar a fila mais cedo, a sua
        # cota "sobra" naturalmente para a(s) outra(s).
        #
        # Quando o total de vagas e impar, a rede que comeca a rodada leva
        # sempre a vaga extra - por isso alternamos quem comeca a cada
        # ciclo (ciclo par/impar), para nao favorecer sistematicamente
        # sempre a mesma rede (tipicamente a primeira em REDES_ATIVAS).
        redes_ordenadas = list(novos_por_rede.keys())
        if ciclo % 2 == 0:
            redes_ordenadas.reverse()
        novos = []
        filas = [novos_por_rede[rede] for rede in redes_ordenadas if novos_por_rede[rede]]
        while filas:
            proxima_rodada = []
            for fila in filas:
                novos.append(fila.pop(0))
                if fila:
                    proxima_rodada.append(fila)
            filas = proxima_rodada

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
                    # Erro GRAVE: nao previsto (as falhas de rede rotineiras
                    # ja sao apanhadas dentro de processar_pool). Notifica -
                    # e o tipo de coisa que vale a pena saber sem estar a
                    # olhar para a consola.
                    alerts.info(f"[red]Erro a processar {pool.get('token_simbolo','?')}:[/red] {e}")
                    try:
                        telegram_alerts.enviar(f"🚨 ERRO ao processar {pool.get('token_simbolo','?')}: {e}")
                    except Exception:
                        pass
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
                # Erro GRAVE: verificar_posicoes ja apanha falhas de cotacao
                # por posicao individualmente - chegar aqui e algo inesperado.
                alerts.info(f"[red]Erro a verificar posicoes:[/red] {e}")
                try:
                    telegram_alerts.enviar(f"🚨 ERRO grave a verificar posicoes: {e}")
                except Exception:
                    pass
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

