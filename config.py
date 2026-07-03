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
POLL_INTERVAL_SEGUNDOS = _env_int("POLL_INTERVAL_SEGUNDOS", 30)
ZONA_AMBIGUA_MIN = _env_int("ZONA_AMBIGUA_MIN", 40)
ZONA_AMBIGUA_MAX = _env_int("ZONA_AMBIGUA_MAX", 70)
LIQUIDEZ_MINIMA_USD = _env_float("LIQUIDEZ_MINIMA_USD", 2000.0)
MAX_ANALISES_POR_CICLO = _env_int("MAX_ANALISES_POR_CICLO", 5)
PAUSA_ENTRE_TOKENS = _env_float("PAUSA_ENTRE_TOKENS", 1.0)
REDE = "solana"

MINT_SOL = "So11111111111111111111111111111111111111112"
MINT_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT_USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
MINTS_BASE_CONHECIDOS = {MINT_SOL, MINT_USDC, MINT_USDT}


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


def fase2_configurada() -> bool:
    return bool(WALLET_PRIVATE_KEY)


def resumo() -> str:
    linhas = [
        f"RPC Solana        : {SOLANA_RPC_URL}",
        f"Intervalo polling : {POLL_INTERVAL_SEGUNDOS}s",
        f"Zona ambigua      : {ZONA_AMBIGUA_MIN}-{ZONA_AMBIGUA_MAX}",
        f"Liquidez minima   : {LIQUIDEZ_MINIMA_USD:.0f} USD",
        f"Camada 1 DeepSeek : {'ON (' + DEEPSEEK_MODEL + ')' if camada1_configurada() else 'OFF (sem chave -> usa score heuristico)'}",
        f"Camada 2 Claude   : {'ON (' + ANTHROPIC_MODEL + ')' if camada2_configurada() else 'OFF (opcional)'}",
        f"Fase 2 (trading)  : {'ON, DRY_RUN=' + str(DRY_RUN) if fase2_configurada() else 'OFF (sem WALLET_PRIVATE_KEY)'}",
    ]
    return "\n".join(linhas)


if __name__ == "__main__":
    print("=== Configuracao carregada ===")
    print(resumo())
