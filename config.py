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

# Endpoints Jupiter (agregador de swaps Solana). Configuraveis (raramente
# precisas de mudar) - so para nao ficarem hardcoded em varios ficheiros
# (executor.py e dashboard.py usavam a mesma URL escrita 2x cada).
JUPITER_QUOTE_URL = _env_texto("JUPITER_QUOTE_URL", "https://public.jupiterapi.com/quote")
JUPITER_SWAP_URL = _env_texto("JUPITER_SWAP_URL", "https://public.jupiterapi.com/swap")

# --- Autenticacao do dashboard ----------------------------------------
# O dashboard controla dinheiro real (compras, vendas, envio de tokens,
# mudanca para modo REAL). Sem password, NAO deve ficar exposto na rede.
# Regra de seguranca (aplicada em dashboard.py):
#   - COM password  -> exige login; pode escutar em 0.0.0.0 (telemovel).
#   - SEM password  -> escuta SO em 127.0.0.1 (localhost), nunca na rede.
# Define uma password forte aqui se quiseres aceder pelo telemovel.
DASHBOARD_PASSWORD = _env_texto("DASHBOARD_PASSWORD", "")


# ==========================================================================
# 2) Camada 1 - Groq (PRINCIPAL) + DeepSeek (fallback) + OpenRouter (3a linha)
# ==========================================================================
# A Groq tenta primeiro (latencia muito baixa); se falhar (erro, rate
# limit, timeout) ou nao tiver chave, cai para a DeepSeek; se essa tambem
# falhar, cai para o OpenRouter (opcional).
GROQ_API_KEY = _env_texto("GROQ_API_KEY")
GROQ_MODEL = _env_texto("GROQ_MODEL", "llama-3.3-70b-versatile")

DEEPSEEK_API_KEY = _env_texto("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _env_texto("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = _env_texto("DEEPSEEK_MODEL", "deepseek-chat")

# 3a linha de fallback (OPCIONAL): OpenRouter da acesso a varios modelos
# com um so endpoint OpenAI-compatible - incluindo modelos GRATIS (o
# default aqui e o Gemini 2.0 Flash, mas troca-se so a variavel, sem
# mexer em codigo, se um tier gratis apertar ou for descontinuado.
OPENROUTER_API_KEY = _env_texto("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _env_texto("OPENROUTER_MODEL", "google/gemini-2.0-flash-exp:free")


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
# --- Filtro de atividade de trading real (traders unicos + volume) -----
# Além da liquidez, exige alguma atividade GENUÍNA na janela mais recente
# (m5, ou m15 se a m5 nao vier) devolvida pela GeckoTerminal em
# "transactions"/"volume_usd" (ver detector.py:_extrair_atividade_recente).
# Aplica-se so aos modos normal e bonding_curve (ver main.py); o Caveira
# ja tem o seu proprio filtro de qualidade, mais apertado, via momentum.py.
# FAIL-OPEN por desenho (ver main.py:_filtro_atividade_recente): se a API
# nao trouxer esta janela para um pool, o filtro NAO rejeita - so avalia
# quando ha dado para avaliar. Diferente do fail-closed usado em
# honeypot/autoridades porque a ausencia aqui nao e um sinal de perigo (o
# token nao fica "mais scam" por a GeckoTerminal ainda nao ter indexado
# a janela), so significa "sem opiniao" - e ha outros filtros (liquidez,
# autoridades, holders) a cobrir o risco de seguranca.
TRADERS_UNICOS_MINIMO = _env_int("TRADERS_UNICOS_MINIMO", 3)
VOLUME_MINIMO_USD = _env_float("VOLUME_MINIMO_USD", 500.0)
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

# --- Circuit breaker do WebSocket (detector_websocket.py) --------------
# O backoff exponencial normal (1,2,4,8,16,30s, satura em 30s) trata bem
# uma queda transitoria - mas se o RPC estiver a rejeitar por um limite
# PERSISTENTE da conta (ex: 429 repetido), continuar a bater a cada 30s
# para sempre so desperdica orcamento de rate-limit e enche o log. Apos
# WS_CIRCUIT_BREAKER_FALHAS 429 CONSECUTIVOS (outros erros de rede nao
# contam - so rejeicoes explicitas do servidor), o detector para de
# tentar durante WS_CIRCUIT_BREAKER_COOLDOWN_SEGUNDOS antes de tentar de
# novo. A deteccao por polling (se METODO_DETECCAO a incluir, o
# recomendado) continua a funcionar normalmente durante o cooldown - so
# se perde a vantagem de latencia do WebSocket, nao a deteccao em si.
WS_CIRCUIT_BREAKER_FALHAS = _env_int("WS_CIRCUIT_BREAKER_FALHAS", 5)
WS_CIRCUIT_BREAKER_COOLDOWN_SEGUNDOS = _env_float("WS_CIRCUIT_BREAKER_COOLDOWN_SEGUNDOS", 300.0)

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
# Conta de tip da Jito (para a compra na bonding curve, que NAO passa pela
# Jupiter - ao contrario do executor.py normal, aqui somos NOS a construir
# a transacao a mao, por isso somos nos a acrescentar a instrucao de
# transferencia do tip). Um dos enderecos publicos oficiais da Jito -
# confirma em jito.wtf/docs se ainda esta correto antes de confiar em
# volume real.
JITO_TIP_ACCOUNT = _env_texto("JITO_TIP_ACCOUNT", "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5")

# --- Alertas Telegram (opcional) ---------------------------------------
# Notificacoes de compra/venda/erro grave/mudanca de saldo via Telegram
# Bot API (gratuita). Ver telegram_alerts.py para o guia de configuracao.
# Sem token/chat_id ou com o toggle desligado, o bot funciona na mesma -
# so nao envia notificacoes (nunca bloqueia por falta disto).
TELEGRAM_ALERTAS_ATIVO = _env_texto("TELEGRAM_ALERTAS_ATIVO", "false").lower() in ("1", "true", "yes", "sim")
TELEGRAM_BOT_TOKEN = _env_texto("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env_texto("TELEGRAM_CHAT_ID", "")
# Uma venda cujo lucro/prejuizo represente >= esta % do saldo atual dispara
# um aviso EXTRA destacado (mudanca "significativa"), alem da notificacao
# normal de venda.
TELEGRAM_ALERTA_SALDO_PCT = _env_float("TELEGRAM_ALERTA_SALDO_PCT", 10.0)

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
        # BUG CORRIGIDO: enderecos Solana (base58) sao sensiveis a
        # maiusculas/minusculas - ao contrario da BSC (hex, 0x...), NAO se
        # pode fazer .lower() aqui. Isto fazia esta comparacao nunca bater
        # certo (detector.py compara sem alterar o case na Solana), e o
        # detetor caia sempre no "else" - por coincidencia quase sempre
        # correto (a convencao da GeckoTerminal costuma por o token novo
        # como base), mas nao por desenho.
        "bases": set(MINTS_BASE_CONHECIDOS),
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

# --- Filtros de COMPRA na BSC (endurecimento pos-diagnostico) -------------
# O diagnostico do prejuizo BSC mostrou que a compra BSC so exigia score
# da IA + liquidez minima generica - sem nenhum bloqueio duro contra o
# golpe n.1 da BSC (honeypot: deixa comprar, nao deixa vender). Estes
# filtros correm TODOS antes de qualquer compra BSC; cada rejeicao fica
# no log e na memoria (memoria/decisoes.jsonl) com o motivo especifico.
# Liquidez minima propria da BSC (acima da generica): pools PancakeSwap
# com liquidez fina sao quase sempre rug descartavel.
LIQUIDEZ_MINIMA_BSC_USD = _env_float("LIQUIDEZ_MINIMA_BSC_USD", 3500.0)
# FAIL-CLOSED: sem confirmacao anti-honeypot (Honeypot.is simula uma
# compra+venda num fork da chain), NAO compra - inclui o caso "API nao
# respondeu/token ainda nao indexado". Poe false para voltar ao antigo.
BSC_EXIGIR_ANTI_HONEYPOT = _env_texto("BSC_EXIGIR_ANTI_HONEYPOT", "true").lower() in ("1", "true", "yes", "sim")
# Tetos de taxa: acima disto a matematica do stop-loss/take-profit fica
# ficticia (vendes "a -20%" mas recebes -30%).
BSC_BUY_TAX_MAX_PCT = _env_float("BSC_BUY_TAX_MAX_PCT", 10.0)
BSC_SELL_TAX_MAX_PCT = _env_float("BSC_SELL_TAX_MAX_PCT", 10.0)
# Concentracao maxima do maior holder (mais apertado que os 30% da
# Solana - a BSC nao tem o "efeito bonding curve" do pump.fun que
# justifica concentracao alta legitima). NOTA: o analyzer_bsc ainda nao
# tem fonte de dados para isto (holders_disponivel=False na BSC), por
# isso o filtro so ativa se/quando essa fonte existir - ver aviso no
# resumo da sessao.
BSC_TOP_HOLDER_MAX_PCT = _env_float("BSC_TOP_HOLDER_MAX_PCT", 20.0)
# % maxima de holders que a Honeypot.is viu FALHAREM a venda (honeypot
# "parcial"/blacklist seletiva) - com dados reais, ao contrario do acima.
BSC_HOLDERS_FALHA_MAX_PCT = _env_float("BSC_HOLDERS_FALHA_MAX_PCT", 20.0)
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

# Sinais on-chain AVANCADOS (liquidez bloqueada via Raydium + historico do
# deployer via Helius) sao os que mais chamadas gastam. Liga/desliga-os
# para conter o custo de RPC/Helius em 24/7 (plano gratuito). Desligados,
# esses sinais ficam "desconhecido"/None (neutros no score), mas o
# essencial (autoridades + holders) continua. Default ON (sem mudanca).
ANALISE_ONCHAIN_AVANCADA = _env_texto("ANALISE_ONCHAIN_AVANCADA", "true").lower() in ("1", "true", "yes", "sim")


# ==========================================================================
# 6) Estado da configuracao (Fase 1)
# ==========================================================================
def camada1_configurada() -> bool:
    """True se pelo menos UM dos tres providers tiver chave (Groq OU
    DeepSeek OU OpenRouter) - so precisas de um deles para a Camada 1
    funcionar."""
    return bool(GROQ_API_KEY) or bool(DEEPSEEK_API_KEY) or bool(OPENROUTER_API_KEY)


def groq_configurado() -> bool:
    return bool(GROQ_API_KEY)


def openrouter_configurado() -> bool:
    return bool(OPENROUTER_API_KEY)


def camada2_configurada() -> bool:
    return bool(ANTHROPIC_API_KEY)


# ==========================================================================
# 7) FASE 2 - Execucao de trades
# ==========================================================================
DRY_RUN = _env_texto("DRY_RUN", "true").lower() in ("1", "true", "yes", "sim")
SALDO_VIRTUAL_INICIAL = _env_float("SALDO_VIRTUAL_INICIAL", 200.0)
WALLET_PRIVATE_KEY = _env_texto("WALLET_PRIVATE_KEY")
# Trava final do envio real na Solana (Jupiter), simetrica a das outras
# vias (PUMPFUN_/BSC_PERMITIR_ENVIO_REAL): mesmo com DRY_RUN=false, as
# compras/vendas normais (Jupiter) so saem on-chain se isto for true.
# Antes disto, o caminho principal era o UNICO sem 2a trava nem simulacao
# - agora corre sempre simulateTransaction e so envia se isto estiver on.
SOLANA_PERMITIR_ENVIO_REAL = _env_texto("SOLANA_PERMITIR_ENVIO_REAL", "false").lower() in ("1", "true", "yes", "sim")
# Quanto tempo (s) esperar pela confirmacao on-chain de uma tx antes de a
# dar como "incerta" (nao abre/fecha a posicao sem confirmacao).
CONFIRMAR_TX_SEGUNDOS = _env_float("CONFIRMAR_TX_SEGUNDOS", 20.0)
MAX_TRADE_USD = _env_float("MAX_TRADE_USD", 5.0)

# --- Dimensionamento DINAMICO por percentagem do saldo -------------------
# O valor de cada compra passa a ser TRADE_PCT_SALDO% do saldo livre NO
# MOMENTO da compra, preso entre TRADE_MIN_USD e TRADE_MAX_USD. Corrige o
# problema do valor fixo: com $200 de saldo uma posicao de $5 era 2.5%,
# mas com $20 ja era 25% - cada trade perdedor mordia uma fatia cada vez
# maior do que restava. Os valores fixos por modo (MAX_TRADE_USD,
# PUMPFUN_MAX_TRADE_USD, SNIPER_RAPIDO_VALOR_USD, BSC_MAX_TRADE_USD)
# continuam a valer como TETO de seguranca de cada modo - os executores
# ja os impoem por dentro; sobe-os no .env se quiseres que um modo
# acompanhe o intervalo dinamico completo.
TRADE_PCT_SALDO = _env_float("TRADE_PCT_SALDO", 2.5)
TRADE_MIN_USD = _env_float("TRADE_MIN_USD", 2.0)
TRADE_MAX_USD = _env_float("TRADE_MAX_USD", 10.0)
SCORE_COMPRA_MAX = _env_int("SCORE_COMPRA_MAX", 20)
# Tokens "fronteira": score acima do limiar de compra mas dentro desta
# margem NAO sao comprados, mas entram na watchlist para decisao manual
SCORE_WATCHLIST_MARGEM = _env_int("SCORE_WATCHLIST_MARGEM", 20)
STOP_LOSS_PCT = _env_float("STOP_LOSS_PCT", 20.0)
TAKE_PROFIT_MULTIPLICADOR = _env_float("TAKE_PROFIT_MULTIPLICADOR", 2.0)
TAKE_PROFIT_VENDER_PCT = _env_float("TAKE_PROFIT_VENDER_PCT", 50.0)
TRAILING_STOP_PCT = _env_float("TRAILING_STOP_PCT", 15.0)

# --- Modo de saida: como sair de uma posicao que ESTA a subir -----------
# "take_profit_parcial" (default, comportamento original): ao atingir
#   TAKE_PROFIT_MULTIPLICADOR, vende TAKE_PROFIT_VENDER_PCT% e so depois
#   ativa o TRAILING_STOP_PCT para o resto.
# "trailing_puro": NUNCA vende parcialmente. So vende 100% quando o preco
#   cair TRAILING_PURO_PCT% do pico mais alto ja atingido (ignora
#   TAKE_PROFIT_MULTIPLICADOR/VENDER_PCT neste modo). Deixa o lucro
#   "correr" enquanto o preco continuar a subir, so sai na reversao.
# Em AMBOS os modos, o STOP_LOSS_PCT normal (desde o preco de COMPRA)
# continua ativo em paralelo, como rede de seguranca se o preco nunca
# chegar a subir.
MODO_SAIDA = _env_texto("MODO_SAIDA", "take_profit_parcial")
TRAILING_PURO_PCT = _env_float("TRAILING_PURO_PCT", 50.0)
SLIPPAGE_BPS = _env_int("SLIPPAGE_BPS", 500)
FICHEIRO_POSICOES = _env_texto("FICHEIRO_POSICOES", "posicoes.json")
INTERVALO_VERIFICAR_POSICOES = _env_int("INTERVALO_VERIFICAR_POSICOES", 20)

# --- Deteccao de reversao (saida antecipada por volume de venda) ------
# Alem do stop-loss/take-profit/trailing normais, deteta se o VOLUME DE
# VENDAS de uma posicao aberta comecar a superar claramente o de compras
# (reutiliza momentum.py, a mesma tecnica do filtro de qualidade do
# Caveira) - sinal de que outros estao a sair, mesmo antes do preco cair
# o suficiente para disparar o stop-loss. DESLIGADO por defeito: e um
# sinal HEURISTICO extra, nao substitui as regras normais, e custa
# chamadas RPC extra por posicao aberta a cada verificacao.
DETECAO_REVERSAO_ATIVA = _env_texto("DETECAO_REVERSAO_ATIVA", "false").lower() in ("1", "true", "yes", "sim")
# Vendas >= este racio x compras dispara a saida antecipada (ex: 2.0 =
# pelo menos o dobro de vendas que compras nas transacoes recentes)
REVERSAO_RATIO_VENDA_MIN = _env_float("REVERSAO_RATIO_VENDA_MIN", 2.0)
# Minimo de vendas observadas antes de confiar no racio (evita disparar
# com 1 venda vs 0 compras, que tecnicamente e um racio "infinito" mas
# nao e informacao suficiente)
REVERSAO_VENDAS_MIN = _env_int("REVERSAO_VENDAS_MIN", 3)

# --- Venda automatica por TEMPO sem valorizacao -------------------------
# Independente do stop-loss/take-profit/trailing: se uma posicao estiver
# aberta ha mais de TEMPO_MAXIMO_SEM_LUCRO_HORAS e o preco atual NAO
# estiver acima do preco de compra (sem lucro nenhum, nem pequeno), vende
# 100% - nao deixa dinheiro parado indefinidamente num token que nao vai
# a lado nenhum. Aplica-se a TODOS os modos de compra. Poe um valor muito
# alto (ex: 999999) para desativar.
TEMPO_MAXIMO_SEM_LUCRO_HORAS = _env_float("TEMPO_MAXIMO_SEM_LUCRO_HORAS", 2.0)


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
# Compra quase instantanea assim que um token e detetado, usando uma
# CHECKLIST BINARIA sim/nao sobre os dados on-chain rapidos (autoridades,
# liquidez, idade - ver abaixo) SEM esperar pela analise da DeepSeek, mais
# o filtro de qualidade opcional por momentum (ver secao seguinte). Nao
# usa o score heuristico (isso e o modo normal) - prioriza velocidade
# sobre a analise completa: o objetivo e apanhar os poucos tokens que
# disparam nos primeiros segundos, aceitando que uma parte das compras
# deste modo vai dar prejuizo pequeno (e o "custo de entrada").
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
# acima disto, vende-se imediatamente - protecao a posteriori. Descido de
# 70 para 50 apos o diagnostico: com um win rate real de 17.6%, a 2a
# camada de protecao so disparava em casos EXTREMOS (score>70) - baixar o
# limiar corta perdas mais cedo em posicoes so "mediocres", nao so nas
# obviamente pessimas.
SNIPER_RAPIDO_SCORE_VENDA_URGENTE = _env_int("SNIPER_RAPIDO_SCORE_VENDA_URGENTE", 50)

# --- Checklist BINARIO do modo caveira (substitui o score heuristico) ---
# Este modo deixou de somar pesos (score). Agora e uma checklist sim/nao,
# mais rapida de avaliar e mais previsivel. Compra SS E SO SS TODAS estas
# forem verdadeiras: (1) mint authority revogada, (2) freeze authority
# revogada, (3) liquidez >= LIQUIDEZ_MINIMA_CAVEIRA_USD, (4) idade do
# token <= IDADE_MAXIMA_CAVEIRA_SEGUNDOS. Qualquer uma que falhe -> nao
# compra neste modo (mas o modo normal, em paralelo, avalia na mesma).
# A liquidez minima do caveira podia ser MAIS BAIXA que a do modo normal
# (aceita-se mais risco em troca de entrar cedo) - mas os resultados reais
# (win rate 17.6%, -13.71% no total) mostraram que $1000 era liquidez
# DEMASIADO fina: pouca margem antes do preco reagir com qualquer venda,
# e mais facil de ser um pool praticamente vazio disfarcado. Subido para
# igualar o minimo do modo normal - a vantagem do Caveira e nao esperar
# pela IA, nao precisa de vir tambem de aceitar liquidez mais fraca.
LIQUIDEZ_MINIMA_CAVEIRA_USD = _env_float("LIQUIDEZ_MINIMA_CAVEIRA_USD", 2000.0)
IDADE_MAXIMA_CAVEIRA_SEGUNDOS = _env_int("IDADE_MAXIMA_CAVEIRA_SEGUNDOS", 60)

# Atraso minimo (segundos) antes de CONFIAR nos dados on-chain (mint/
# freeze authority) para a checklist do Caveira. Diagnostico real: com
# deteccao WebSocket (0-2s de vida do token), o RPC ainda nao indexou a
# conta do mint - getAccountInfo devolve "conta nao existe", o que
# _passa_checklist_caveira le como onchain_disponivel=False e reprova
# por fail-closed (comportamento correto, mas desperdicado - nao e um
# problema real do token, e so o RPC ainda nao ter apanhado o jeito).
# Se o token for mais novo do que isto, o Caveira AGENDA uma unica
# retentativa (ver _caveira_pendentes/_reprocessar_caveira_pendentes em
# main.py) em vez de rejeitar logo - sem sleep bloqueante, o proprio
# ciclo principal (que corre a cada 2s com WebSocket ativo) reprocessa a
# fila. Nao consome da janela de IDADE_MAXIMA_CAVEIRA_SEGUNDOS mais do
# que o necessario: 6-8s costuma ser suficiente para o RPC indexar.
CAVEIRA_ATRASO_MINIMO_SEGUNDOS = _env_float("CAVEIRA_ATRASO_MINIMO_SEGUNDOS", 6.0)

# --- Filtro de QUALIDADE do Caveira (momentum.py) ----------------------
# A checklist binaria acima (autoridades+liquidez+idade) filtra scams
# TECNICOS, mas nao filtra qualidade: a maioria dos tokens do pump.fun
# passa essas 4 condicoes nos primeiros segundos, incluindo os que vao
# morrer sem ninguem comprar. Estes sinais extra pedem alguma atividade
# REAL (compras > vendas, varios compradores distintos, holder nao
# concentrado, algumas transacoes ja feitas) antes de entrar.
# TRADE-OFF DELIBERADO: para reunir estes dados esperamos alguns segundos
# apos a deteccao (CAVEIRA_JANELA_MOMENTUM_SEGUNDOS) e fazemos chamadas
# RPC extra - o Caveira fica mais LENTO mas mais SELETIVO. Cada sinal
# desliga-se individualmente pondo o valor a 0 (ou 100 no caso do
# top-holder) se preferires velocidade pura (comportamento antigo).
#
# DIAGNOSTICO (apos os primeiros resultados reais: 68 compras, 51 vendas,
# win rate 17.6%, -$27.41/-13.71%): os limiares abaixo eram permissivos
# demais para o problema real, que NAO e "scams tecnicos" mas sim
# CONTAMINACAO POR OUTROS BOTS DE SNIPE. Nos primeiros segundos de um
# token novo no pump.fun, quem mais compra sao OUTROS bots de sniper (ha
# dezenas a disparar em cada lancamento), nao interesse organico humano.
# "3 compradores distintos" ou "5 transacoes" e um limiar que qualquer
# token, bom ou mau, atinge trivialmente so com esses bots - dava falsa
# confianca sem filtrar nada de facto. Alem disso, o fail-open (ver
# main.py) fazia o filtro nao bloquear quase nada num RPC publico,
# custando so o atraso sem ganhar seletividade real. Os valores abaixo
# foram apertados para exigir MUITO mais atividade antes de confiar nela,
# e o main.py passou a FAIL-CLOSED (rejeita se nao conseguir confirmar).
CAVEIRA_JANELA_MOMENTUM_SEGUNDOS = _env_float("CAVEIRA_JANELA_MOMENTUM_SEGUNDOS", 8.0)
# Minimo de compras por cada venda (ex: 3.0 = pelo menos o triplo de
# compras que vendas). 0 desliga este filtro.
CAVEIRA_RATIO_COMPRA_VENDA_MIN = _env_float("CAVEIRA_RATIO_COMPRA_VENDA_MIN", 3.0)
# Minimo de enderecos DISTINTOS a comprar (evita 1-2 wallets, ou so um
# punhado de bots de sniper, a inflacionar artificialmente o volume).
# 0 desliga este filtro.
CAVEIRA_COMPRADORES_UNICOS_MIN = _env_int("CAVEIRA_COMPRADORES_UNICOS_MIN", 6)
# Se o maior holder detiver mais do que isto (%), rejeita mesmo que passe
# tudo o resto. Reutiliza o dado que o analyzer.py ja calculou (sem
# chamadas RPC extra). <= 0 desliga este filtro.
CAVEIRA_TOP_HOLDER_MAX_PCT = _env_float("CAVEIRA_TOP_HOLDER_MAX_PCT", 30.0)
# Minimo de transacoes ja feitas desde a criacao (evita comprar no
# proprio bloco de criacao, antes de qualquer interesse real). 0 desliga.
CAVEIRA_TRANSACOES_MIN = _env_int("CAVEIRA_TRANSACOES_MIN", 12)


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
        TRADE_MAX_USD,  # teto do dimensionamento dinamico por % do saldo
    ]
    return max(maiores_limites) * 50


# ==========================================================================
# 7d) Envio de tokens SPL da carteira (dashboard) - acao com dinheiro real
# ==========================================================================
# Enviar tokens e IRREVERSIVEL. Como no pump.fun e na BSC, o envio
# on-chain em modo REAL so acontece se esta trava for explicitamente
# ligada (alem do CONFIRMO exigido no pedido). Em DRY_RUN nunca envia.
PERMITIR_ENVIO_TOKENS = _env_texto("PERMITIR_ENVIO_TOKENS", "false").lower() in ("1", "true", "yes", "sim")


def _status_camada1() -> str:
    """Cadeia de fallback configurada da Camada 1, para o banner de
    arranque (resumo()). Mostra so os providers com chave, na ordem em
    que analisar_token() os tenta."""
    providers = []
    if GROQ_API_KEY:
        providers.append(f"Groq ({GROQ_MODEL})")
    if DEEPSEEK_API_KEY:
        providers.append(f"DeepSeek ({DEEPSEEK_MODEL})")
    if OPENROUTER_API_KEY:
        providers.append(f"OpenRouter ({OPENROUTER_MODEL})")
    if not providers:
        return "OFF (sem chave -> usa score heuristico)"
    return " -> ".join(providers)


def resumo() -> str:
    linhas = [
        f"RPC Solana        : {SOLANA_RPC_URL}",
        f"Redes ativas      : {', '.join(REDES_ATIVAS)}",
        f"Metodo deteccao   : {METODO_DETECCAO}",
        f"Intervalo polling : {POLL_INTERVAL_SEGUNDOS}s",
        f"Zona ambigua      : {ZONA_AMBIGUA_MIN}-{ZONA_AMBIGUA_MAX}",
        f"Liquidez minima   : {LIQUIDEZ_MINIMA_USD:.0f} USD",
        f"Camada 1 IA       : {_status_camada1()}",
        f"Camada 2 Claude   : {'ON (' + ANTHROPIC_MODEL + ')' if camada2_configurada() else 'OFF (opcional)'}",
        f"Fase 2 (trading)  : {'ON, DRY_RUN=' + str(DRY_RUN) if fase2_configurada() else 'OFF (sem WALLET_PRIVATE_KEY)'}",
        f"Envio real Solana : {'PERMITIDO' if SOLANA_PERMITIR_ENVIO_REAL else 'BLOQUEADO (so simula; liga SOLANA_PERMITIR_ENVIO_REAL)'}",
        f"Envio Jito        : {'ON (tip ' + str(JITO_TIP_LAMPORTS) + ' lamports)' if JITO_ATIVO else 'OFF (envio normal pelo RPC)'}",
        f"Copy Trading      : {'ON (' + str(len([w for w in COPY_TRADE_WALLETS.split(',') if w.strip()])) + ' carteira(s))' if COPY_TRADE_ATIVO else 'OFF'}",
    ]
    return "\n".join(linhas)


if __name__ == "__main__":
    print("=== Configuracao carregada ===")
    print(resumo())
