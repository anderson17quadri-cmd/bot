"""
config.py
=========
Ponto UNICO de configuracao do bot.

Aqui NAO ha chaves escritas a mao. Tudo o que e sensivel (chaves de API)
ou que possa mudar (RPC, thresholds) vem do ficheiro .env, que e lido no
arranque com a biblioteca python-dotenv.

Regra de ouro: se algum dia precisares de mudar um valor, mudas no .env,
nunca aqui no codigo.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _env_texto(nome: str, defeito: str = "") -> str:
    valor = os.getenv(nome, defeito)
    return valor.strip() if valor else defeito


def _env_int(nome: str, defeito: int) -> int:
    try:
        return int(os.getenv(nome, str(defeito)))
    except (TypeError, ValueError):
        return defeito


def _env_float(nome: str, defeito: float) -> float:
    try:
        return float(os.getenv(nome, str(defeito)))
    except (TypeError, ValueError):
        return defeito


# ==========================================================================
# 1) RPC da Solana
# ==========================================================================
SOLANA_RPC_URL = _env_texto("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


# ==========================================================================
# 2) Camada 1 - DeepSeek
# ==========================================================================
DEEPSEEK_API_KEY = _env_texto("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _env_texto("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = _env_texto("DEEPSEEK_MODEL", "deepseek-chat")


# ==========================================================================
# 3) Camada 2 - Claude (OPCIONAL)
# ==========================================================================
ANTHROPIC_API_KEY = _env_texto("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env_texto("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")


# ==========================================================================
# 4) Comportamento do bot
# ==========================================================================
# NOTA sobre baixar isto muito (ex: 10-15s): a GeckoTerminal tem o seu
# proprio ritmo de atualizacao da lista "new_pools", que nao e garantido
# ser mais rapido do que isso - pedir com mais frequencia nao traz
# necessariamente tokens genuinamente mais novos, so aumenta a chance de
# reencontrares o MESMO token por vias diferentes (ver cooldown.py: um
# token pode aparecer com pool_address diferente quando migra de
# bonding curve para um pool normal, por exemplo). O cooldown de
# reanalise (COOLDOWN_REANALISE_MINUTOS) evita o desperdicio de chamadas
# a IA nesses casos, mas nao faz a GeckoTerminal ir mais depressa.
POLL_INTERVAL_SEGUNDOS = _env_int("POLL_INTERVAL_SEGUNDOS", 30)
ZONA_AMBIGUA_MIN = _env_int("ZONA_AMBIGUA_MIN", 40)
ZONA_AMBIGUA_MAX = _env_int("ZONA_AMBIGUA_MAX", 70)
LIQUIDEZ_MINIMA_USD = _env_float("LIQUIDEZ_MINIMA_USD", 2000.0)
MAX_ANALISES_POR_CICLO = _env_int("MAX_ANALISES_POR_CICLO", 5)
PAUSA_ENTRE_TOKENS = _env_float("PAUSA_ENTRE_TOKENS", 1.0)
# Um mint ja analisado (heuristico + IA) nao volta a ser processado
# dentro desta janela, mesmo que a GeckoTerminal o devolva de novo com
# um pool_address diferente (ex: migracao de bonding curve). NAO se
# aplica a posicoes ja abertas - essas sao verificadas pelo seu proprio
# ciclo (verificar_posicoes), independente disto.
COOLDOWN_REANALISE_MINUTOS = _env_int("COOLDOWN_REANALISE_MINUTOS", 10)

# --- Metodo de deteccao: polling (GeckoTerminal) vs websocket (Helius) ---
# "polling"   -> o detector.py de sempre, pergunta a GeckoTerminal de X em
#                X segundos. Fiavel, funciona em qualquer RPC, so-Solana+BSC.
# "websocket" -> o detector_websocket.py (logsSubscribe do Helius): recebe
#                um aviso INSTANTANEO assim que um token pump.fun e criado,
#                em vez de perguntar. Muito mais rapido a SABER do token,
#                mas experimental e so pump.fun/Solana. Precisa de um RPC
#                que suporte WebSocket (Helius suporta; o publico nao).
# Por defeito "polling" (o que ja funciona). So muda se ligares de
# proposito. Podes por os dois: "polling,websocket" corre ambos.
METODO_DETECCAO = _env_texto("METODO_DETECCAO", "polling")

# WebSocket do RPC: derivado do SOLANA_RPC_URL trocando http->ws. Se o teu
# RPC tiver um endpoint WS diferente, define-o aqui explicitamente.
SOLANA_WS_URL = _env_texto("SOLANA_WS_URL", "")

# Rate limiter (token bucket) para as chamadas RPC extra do websocket -
# nunca ultrapassar o plano gratuito do Helius. Pedidos por segundo max.
RPC_MAX_PEDIDOS_POR_SEGUNDO = _env_float("RPC_MAX_PEDIDOS_POR_SEGUNDO", 8.0)

# --- Otimizacoes de execucao -------------------------------------------
# Cache do preco do SOL (segundos). O preco quase nao mexe em poucos
# segundos, mas obter_preco_sol_usd() e chamado em cada compra (caminho
# critico). Cache curta tira essa latencia sem arriscar preco velho.
PRECO_SOL_CACHE_SEGUNDOS = _env_float("PRECO_SOL_CACHE_SEGUNDOS", 8.0)

# --- Jito (envio prioritario de transacoes) ----------------------------
# Em vez de mandar a transacao pelo RPC normal, manda-a ao block-engine
# da Jito com uma "gorjeta" (tip). Os validadores da Jito priorizam quem
# paga tip -> a transacao entra mais depressa nos momentos de congestao.
# So corre em modo REAL (DRY_RUN=False); no simulado e completamente
# ignorado. DESLIGADO por defeito - liga so quando quiseres pagar o tip.
JITO_ATIVO = _env_texto("JITO_ATIVO", "false").lower() in ("1", "true", "yes", "sim")
# Endpoint publico do block-engine (sem conta/chave). Ha varias regioes;
# esta e a global. Podes trocar por ex. amsterdam/frankfurt/ny/tokyo.
JITO_BLOCK_ENGINE_URL = _env_texto(
    "JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf"
)
# Valor do tip em lamports (1 SOL = 1e9). O minimo aceite pela Jito e
# 1000 lamports; um valor demasiado baixo raramente e includo. 100000
# lamports (~0.0001 SOL) e um ponto de partida razoavel.
JITO_TIP_LAMPORTS = _env_int("JITO_TIP_LAMPORTS", 100_000)

# --- Copy Trading (seguir carteiras "top") -----------------------------
# Segue uma lista MANUAL de carteiras; quando uma delas COMPRA um token, o
# bot replica (valor pequeno e fixo) apos uma verificacao minima. E um 4o
# caminho de compra dedicado, isolado dos outros 3. DESLIGADO por defeito.
COPY_TRADE_ATIVO = _env_texto("COPY_TRADE_ATIVO", "false").lower() in ("1", "true", "yes", "sim")
# Carteiras a seguir, separadas por virgula (enderecos Solana base58).
COPY_TRADE_WALLETS = _env_texto("COPY_TRADE_WALLETS", "")
# Valor (USD) de cada compra copiada.
COPY_TRADE_VALOR_USD = _env_float("COPY_TRADE_VALOR_USD", 1.0)
# Limite diario OBRIGATORIO (USD) - conter o dano de copiar um dia mau.
COPY_TRADE_LIMITE_DIARIO_USD = _env_float("COPY_TRADE_LIMITE_DIARIO_USD", 10.0)
# De quantos em quantos segundos sondar cada carteira por transacoes novas.
COPY_TRADE_INTERVALO_SEGUNDOS = _env_int("COPY_TRADE_INTERVALO_SEGUNDOS", 5)

# --- Multi-chain (Parte B) ---------------------------------------------
# REDE fica como a rede "principal"/legada (Solana) para o codigo antigo
# que ainda a referencia. REDES_ATIVAS e a lista de redes a monitorizar
# em paralelo: por defeito so a Solana; poe "solana,bsc" no .env para
# ligar a BSC. So aceitamos redes que sabemos tratar.
REDE = "solana"
_REDES_SUPORTADAS = {"solana", "bsc"}
REDES_ATIVAS = [
    r.strip().lower()
    for r in _env_texto("REDES_ATIVAS", "solana").split(",")
    if r.strip().lower() in _REDES_SUPORTADAS
] or ["solana"]

# Tokens-base (o "outro lado" do par: moeda/estveis) por chain. Servem
# para o detector saber qual dos dois lados do par e o TOKEN NOVO.
MINT_SOL = "So11111111111111111111111111111111111111112"
MINT_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT_USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
MINTS_BASE_CONHECIDOS = {MINT_SOL, MINT_USDC, MINT_USDT}  # Solana (legado)

# BSC (enderecos Ethereum 0x..., em minusculas para comparar sem falhas)
BSC_WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
BSC_USDT = "0x55d398326f99059ff775485246999027b3197955"
BSC_BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
BSC_USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"

# Mapa por chain: rede GeckoTerminal, prefixo dos ids e tokens-base
CHAINS = {
    "solana": {
        "gecko": "solana",
        "prefixo": "solana_",
        "bases": {m.lower() for m in MINTS_BASE_CONHECIDOS},
        "moeda": "SOL",
    },
    "bsc": {
        "gecko": "bsc",
        "prefixo": "bsc_",
        "bases": {BSC_WBNB, BSC_USDT, BSC_BUSD, BSC_USDC},
        "moeda": "BNB",
    },
}

# RPC da BSC (leitura + envio de transacoes)
BSC_RPC_URL = _env_texto("BSC_RPC_URL", "https://bsc-dataseed.binance.org")
BSC_CHAIN_ID = 56  # id da rede BSC (usado ao assinar transacoes)

# Router da PancakeSwap V2 (o AMM mais usado na BSC)
PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"

# Wallet BSC (formato Ethereum 0x...) - SEPARADA da Solana, nunca a mesma chave
WALLET_PRIVATE_KEY_BSC = _env_texto("WALLET_PRIVATE_KEY_BSC")
# Limite por trade na BSC, independente da Solana (mas o mesmo default $5)
BSC_MAX_TRADE_USD = _env_float("BSC_MAX_TRADE_USD", 5.0)
# Trava final do envio real na BSC (como no pump.fun): mesmo com
# DRY_RUN=false, so envia on-chain se isto for true. O construtor da
# transacao foi escrito e simulado, mas NAO validado com um swap real.
BSC_PERMITIR_ENVIO_REAL = _env_texto("BSC_PERMITIR_ENVIO_REAL", "false").lower() in ("1", "true", "yes", "sim")


def fase2_bsc_configurada() -> bool:
    return bool(WALLET_PRIVATE_KEY_BSC)


# ==========================================================================
# 5) Pesos do score heuristico
# ==========================================================================
PESO_FREEZE_AUTHORITY = 30
PESO_MINT_AUTHORITY = 25
PESO_HOLDER_ALTO = 25
PESO_HOLDER_MEDIO = 12
PESO_LIQUIDEZ_BAIXA = 20
PESO_LIQUIDEZ_MEDIA = 10

LIMITE_HOLDER_ALTO = 50.0
LIMITE_HOLDER_MEDIO = 30.0

# --- Sinais avancados (liquidez bloqueada + historico do deployer) ---
# LP numa carteira normal = o criador pode retirar a liquidez a qualquer
# momento (rug pull classico) -> penalizacao forte
PESO_LIQUIDEZ_NAO_BLOQUEADA = _env_int("PESO_LIQUIDEZ_NAO_BLOQUEADA", 30)
# LP queimado/bloqueado = o sinal MAIS FORTE de seguranca -> reduz o score
# (valor negativo: e um bonus, nao uma penalizacao)
BONUS_LIQUIDEZ_BLOQUEADA = _env_int("BONUS_LIQUIDEZ_BLOQUEADA", -20)
# Deployer que criou muitos tokens nas ultimas horas = scam em serie
PESO_DEPLOYER_SERIAL = _env_int("PESO_DEPLOYER_SERIAL", 25)
LIMITE_DEPLOYER_TOKENS = _env_int("LIMITE_DEPLOYER_TOKENS", 5)   # mais do que isto = suspeito
DEPLOYER_JANELA_HORAS = _env_int("DEPLOYER_JANELA_HORAS", 48)    # janela de contagem
# Liquidez "ja alta demais" para a idade do pool (sinal informativo,
# possivel inflacao artificial antes de um pump)
LIQUIDEZ_SUSPEITA_USD = _env_float("LIQUIDEZ_SUSPEITA_USD", 25000.0)
IDADE_SUSPEITA_MINUTOS = _env_float("IDADE_SUSPEITA_MINUTOS", 5.0)


# ==========================================================================
# 6) Estado da configuracao (Fase 1)
# ==========================================================================
def camada1_configurada() -> bool:
    return bool(DEEPSEEK_API_KEY)


def camada2_configurada() -> bool:
    return bool(ANTHROPIC_API_KEY)


# ==========================================================================
# 7) FASE 2 - Execucao de trades
# ==========================================================================
DRY_RUN = _env_texto("DRY_RUN", "true").lower() in ("1", "true", "yes", "sim")
SALDO_VIRTUAL_INICIAL = _env_float("SALDO_VIRTUAL_INICIAL", 200.0)
WALLET_PRIVATE_KEY = _env_texto("WALLET_PRIVATE_KEY")
MAX_TRADE_USD = _env_float("MAX_TRADE_USD", 5.0)
SCORE_COMPRA_MAX = _env_int("SCORE_COMPRA_MAX", 20)
# Tokens "fronteira": score acima do limiar de compra mas dentro desta
# margem NAO sao comprados, mas entram na watchlist para decisao manual
SCORE_WATCHLIST_MARGEM = _env_int("SCORE_WATCHLIST_MARGEM", 20)
STOP_LOSS_PCT = _env_float("STOP_LOSS_PCT", 20.0)
TAKE_PROFIT_MULTIPLICADOR = _env_float("TAKE_PROFIT_MULTIPLICADOR", 2.0)
TAKE_PROFIT_VENDER_PCT = _env_float("TAKE_PROFIT_VENDER_PCT", 50.0)
TRAILING_STOP_PCT = _env_float("TRAILING_STOP_PCT", 15.0)
SLIPPAGE_BPS = _env_int("SLIPPAGE_BPS", 500)
FICHEIRO_POSICOES = _env_texto("FICHEIRO_POSICOES", "posicoes.json")
INTERVALO_VERIFICAR_POSICOES = _env_int("INTERVALO_VERIFICAR_POSICOES", 20)


# ==========================================================================
# 7b) BONDING CURVE do pump.fun (Parte C) - EXPERIMENTAL e ARRISCADO
# ==========================================================================
# Comprar tokens AINDA na bonding curve (antes de migrarem para um pool
# normal). A esmagadora maioria destes tokens NUNCA gradua - risco de
# perda total muito maior. Por isso: desligado por defeito, limite de
# trade mais baixo, limiar de score mais apertado e atraso minimo antes
# de comprar (evita o instante do lancamento, onde estao os piores scams).
PUMPFUN_BONDING_CURVE_ATIVO = _env_texto("PUMPFUN_BONDING_CURVE_ATIVO", "false").lower() in ("1", "true", "yes", "sim")
PUMPFUN_MAX_TRADE_USD = _env_float("PUMPFUN_MAX_TRADE_USD", 2.0)          # < MAX_TRADE_USD normal
PUMPFUN_SCORE_COMPRA_MAX = _env_int("PUMPFUN_SCORE_COMPRA_MAX", 10)       # mais apertado que SCORE_COMPRA_MAX
PUMPFUN_ATRASO_MINIMO_SEGUNDOS = _env_int("PUMPFUN_ATRASO_MINIMO_SEGUNDOS", 45)
# Trava final de envio real: mesmo com DRY_RUN=false, o envio on-chain da
# compra na curva so acontece se isto for explicitamente true. O construtor
# da transacao foi escrito a partir do IDL oficial mas NAO foi validado com
# uma compra real em mainnet - esta trava evita disparos acidentais.
PUMPFUN_PERMITIR_ENVIO_REAL = _env_texto("PUMPFUN_PERMITIR_ENVIO_REAL", "false").lower() in ("1", "true", "yes", "sim")


# ==========================================================================
# 7c) MODO SNIPER RAPIDO ("modo caveira") - O MAIS ARRISCADO DE TODOS
# ==========================================================================
# Compra quase instantanea assim que um token e detetado, usando SO as
# verificacoes on-chain rapidas que ja existem (mint/freeze authority,
# liquidez minima) - SEM esperar pela analise da DeepSeek. Aceita scores
# heuristicos muito mais altos (ate 50, contra 20 do modo normal) porque
# prioriza velocidade sobre seguranca: o objetivo e apanhar os poucos
# tokens que disparam nos primeiros segundos, aceitando que a maioria das
# compras deste modo vai dar prejuizo pequeno (e o "custo de entrada").
# A analise completa (DeepSeek) continua a correr por tras; se vier um
# score mau para uma posicao comprada por este modo, ela e vendida de
# imediato (a IA funciona aqui como uma 2a camada de protecao, depois
# da compra em vez de antes).
#
# Desligado por defeito, SEMPRE. O limite diario e OBRIGATORIO (nao best
# effort) - existe precisamente para conter o dano maximo possivel se o
# modo ficar ligado durante um dia mau.
SNIPER_RAPIDO_ATIVO = _env_texto("SNIPER_RAPIDO_ATIVO", "false").lower() in ("1", "true", "yes", "sim")
SNIPER_RAPIDO_VALOR_USD = _env_float("SNIPER_RAPIDO_VALOR_USD", 1.0)       # compra minuscula, so este modo
SNIPER_RAPIDO_LIMITE_DIARIO_USD = _env_float("SNIPER_RAPIDO_LIMITE_DIARIO_USD", 10.0)
# Se a DeepSeek (depois de a posicao ja estar comprada) devolver um score
# acima disto, vende-se imediatamente - protecao a posteriori
SNIPER_RAPIDO_SCORE_VENDA_URGENTE = _env_int("SNIPER_RAPIDO_SCORE_VENDA_URGENTE", 70)

# --- Checklist BINARIO do modo caveira (substitui o score heuristico) ---
# Este modo deixou de somar pesos (score). Agora e uma checklist sim/nao,
# mais rapida de avaliar e mais previsivel. Compra SS E SO SS TODAS estas
# forem verdadeiras: (1) mint authority revogada, (2) freeze authority
# revogada, (3) liquidez >= LIQUIDEZ_MINIMA_CAVEIRA_USD, (4) idade do
# token <= IDADE_MAXIMA_CAVEIRA_SEGUNDOS. Qualquer uma que falhe -> nao
# compra neste modo (mas o modo normal, em paralelo, avalia na mesma).
# A liquidez minima do caveira pode ser MAIS BAIXA que a do modo normal
# (aceita-se mais risco em troca de entrar cedo).
LIQUIDEZ_MINIMA_CAVEIRA_USD = _env_float("LIQUIDEZ_MINIMA_CAVEIRA_USD", 1000.0)
IDADE_MAXIMA_CAVEIRA_SEGUNDOS = _env_int("IDADE_MAXIMA_CAVEIRA_SEGUNDOS", 60)


def fase2_configurada() -> bool:
    return bool(WALLET_PRIVATE_KEY)


def limite_sanidade_trade_usd() -> float:
    """Limite de seguranca usado por carteira.py: REJEITA qualquer
    alteracao de saldo (compra ou venda) maior do que isto, mesmo em
    modo simulado. Protege contra bugs de unidades (ex: quantidade de
    tokens, um numero na casa dos milhares de milhoes, usada por engano
    como se fosse valor em USD) - o tipo de bug que ja aconteceu aqui.

    E deliberadamente generoso (50x o maior limite de trade configurado
    entre os 3 modos de compra) para NUNCA bloquear uma venda legitima
    com lucro grande - so existe para apanhar corrupcoes de ordens de
    grandeza, nao para policiar o dia a dia normal do bot."""
    maiores_limites = [
        MAX_TRADE_USD, PUMPFUN_MAX_TRADE_USD, BSC_MAX_TRADE_USD, SNIPER_RAPIDO_VALOR_USD,
    ]
    return max(maiores_limites) * 50


# ==========================================================================
# 7c) Envio de tokens SPL da carteira (dashboard) - acao com dinheiro real
# ==========================================================================
# Enviar tokens e IRREVERSIVEL. Como no pump.fun e na BSC, o envio
# on-chain em modo REAL so acontece se esta trava for explicitamente
# ligada (alem do CONFIRMO exigido no pedido). Em DRY_RUN nunca envia.
PERMITIR_ENVIO_TOKENS = _env_texto("PERMITIR_ENVIO_TOKENS", "false").lower() in ("1", "true", "yes", "sim")


def resumo() -> str:
    linhas = [
        f"RPC Solana        : {SOLANA_RPC_URL}",
        f"Redes ativas      : {', '.join(REDES_ATIVAS)}",
        f"Metodo deteccao   : {METODO_DETECCAO}",
        f"Intervalo polling : {POLL_INTERVAL_SEGUNDOS}s",
        f"Zona ambigua      : {ZONA_AMBIGUA_MIN}-{ZONA_AMBIGUA_MAX}",
        f"Liquidez minima   : {LIQUIDEZ_MINIMA_USD:.0f} USD",
        f"Camada 1 DeepSeek : {'ON (' + DEEPSEEK_MODEL + ')' if camada1_configurada() else 'OFF (sem chave -> usa score heuristico)'}",
        f"Camada 2 Claude   : {'ON (' + ANTHROPIC_MODEL + ')' if camada2_configurada() else 'OFF (opcional)'}",
        f"Fase 2 (trading)  : {'ON, DRY_RUN=' + str(DRY_RUN) if fase2_configurada() else 'OFF (sem WALLET_PRIVATE_KEY)'}",
        f"Envio Jito        : {'ON (tip ' + str(JITO_TIP_LAMPORTS) + ' lamports)' if JITO_ATIVO else 'OFF (envio normal pelo RPC)'}",
        f"Copy Trading      : {'ON (' + str(len([w for w in COPY_TRADE_WALLETS.split(',') if w.strip()])) + ' carteira(s))' if COPY_TRADE_ATIVO else 'OFF'}",
    ]
    return "\n".join(linhas)


if __name__ == "__main__":
    print("=== Configuracao carregada ===")
    print(resumo())
