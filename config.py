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

# Le o ficheiro .env (se existir) e coloca as variaveis no os.environ.
# Se o .env nao existir, nao ha problema: usamos os valores por defeito.
load_dotenv()


# --------------------------------------------------------------------------
# Funcoes auxiliares para ler variaveis de ambiente com um valor por defeito
# e ja convertidas para o tipo certo (int / float).
# Assim, se alguem escrever algo invalido no .env, nao rebenta tudo.
# --------------------------------------------------------------------------
def _env_texto(nome: str, defeito: str = "") -> str:
    """Le uma variavel de ambiente como texto (string)."""
    valor = os.getenv(nome, defeito)
    # .strip() remove espacos/enter acidentais no inicio/fim
    return valor.strip() if valor else defeito


def _env_int(nome: str, defeito: int) -> int:
    """Le uma variavel de ambiente e converte para inteiro."""
    try:
        return int(os.getenv(nome, str(defeito)))
    except (TypeError, ValueError):
        # Se o valor no .env nao for um numero valido, usamos o defeito
        return defeito


def _env_float(nome: str, defeito: float) -> float:
    """Le uma variavel de ambiente e converte para numero decimal."""
    try:
        return float(os.getenv(nome, str(defeito)))
    except (TypeError, ValueError):
        return defeito


# ==========================================================================
# 1) RPC da Solana
# ==========================================================================
# URL do no RPC. Comeca no publico gratuito; troca para Helius/QuickNode
# apenas mudando esta variavel no .env.
SOLANA_RPC_URL = _env_texto("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


# ==========================================================================
# 2) Camada 1 - DeepSeek (analise primaria, corre para TODOS os tokens)
# ==========================================================================
DEEPSEEK_API_KEY = _env_texto("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _env_texto("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = _env_texto("DEEPSEEK_MODEL", "deepseek-chat")


# ==========================================================================
# 3) Camada 2 - Claude (so corre na zona ambigua). OPCIONAL.
# ==========================================================================
ANTHROPIC_API_KEY = _env_texto("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env_texto("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")


# ==========================================================================
# 4) Comportamento do bot
# ==========================================================================
# Intervalo (segundos) entre cada procura de novos pools
POLL_INTERVAL_SEGUNDOS = _env_int("POLL_INTERVAL_SEGUNDOS", 30)

# Zona "ambigua" do score da Camada 1 que dispara a Camada 2 (Claude).
# Convencao de score: 0 = seguro  ...  100 = perigoso/scam.
# Se o score da Camada 1 ficar ENTRE estes dois valores, chamamos o Claude.
ZONA_AMBIGUA_MIN = _env_int("ZONA_AMBIGUA_MIN", 40)
ZONA_AMBIGUA_MAX = _env_int("ZONA_AMBIGUA_MAX", 70)

# Liquidez minima (USD) considerada saudavel. Abaixo disto -> soma risco.
LIQUIDEZ_MINIMA_USD = _env_float("LIQUIDEZ_MINIMA_USD", 2000.0)

# Maximo de tokens a analisar por ciclo. Protege o RPC publico de rajadas
# (aparecem muitos pools por minuto; nao vale a pena analisar tudo de uma vez).
MAX_ANALISES_POR_CICLO = _env_int("MAX_ANALISES_POR_CICLO", 5)

# Pausa (segundos) entre analisar um token e o seguinte, para ser "educado"
# com o RPC e com as APIs de IA.
PAUSA_ENTRE_TOKENS = _env_float("PAUSA_ENTRE_TOKENS", 1.0)

# Rede a monitorizar na API de deteccao (GeckoTerminal usa "solana")
REDE = "solana"

# Mints "de referencia" (SOL / stablecoins). Servem para descobrir qual e o
# token NOVO num par (o outro lado do par costuma ser um destes).
MINT_SOL = "So11111111111111111111111111111111111111112"
MINT_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT_USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
MINTS_BASE_CONHECIDOS = {MINT_SOL, MINT_USDC, MINT_USDT}


# ==========================================================================
# 5) Pesos do score heuristico (calculado ANTES da IA, em analyzer.py)
#    Podes afinar estes valores a vontade. Convencao: soma-se RISCO.
# ==========================================================================
PESO_FREEZE_AUTHORITY = 30   # existe autoridade de freeze -> podem congelar a tua carteira
PESO_MINT_AUTHORITY = 25     # existe autoridade de mint   -> podem imprimir mais tokens
PESO_HOLDER_ALTO = 25        # 1 holder detem > LIMITE_HOLDER_ALTO %
PESO_HOLDER_MEDIO = 12       # 1 holder detem > LIMITE_HOLDER_MEDIO %
PESO_LIQUIDEZ_BAIXA = 20     # liquidez < LIQUIDEZ_MINIMA_USD
PESO_LIQUIDEZ_MEDIA = 10     # liquidez < 2x a minima

LIMITE_HOLDER_ALTO = 50.0    # em %
LIMITE_HOLDER_MEDIO = 30.0   # em %


# ==========================================================================
# 6) Funcoes de "estado da configuracao" - usadas no arranque (main.py)
#    para avisar o utilizador do que esta ou nao configurado.
# ==========================================================================
def camada1_configurada() -> bool:
    """True se a chave da DeepSeek (Camada 1) estiver preenchida."""
    return bool(DEEPSEEK_API_KEY)


def camada2_configurada() -> bool:
    """True se a chave do Claude (Camada 2) estiver preenchida."""
    return bool(ANTHROPIC_API_KEY)


def resumo() -> str:
    """Devolve um pequeno resumo (texto) do estado da configuracao.

    Nunca imprime as chaves - so diz se estao 'ON' ou 'OFF'.
    """
    linhas = [
        f"RPC Solana        : {SOLANA_RPC_URL}",
        f"Intervalo polling : {POLL_INTERVAL_SEGUNDOS}s",
        f"Zona ambigua      : {ZONA_AMBIGUA_MIN}-{ZONA_AMBIGUA_MAX}",
        f"Liquidez minima   : {LIQUIDEZ_MINIMA_USD:.0f} USD",
        f"Camada 1 DeepSeek : {'ON (' + DEEPSEEK_MODEL + ')' if camada1_configurada() else 'OFF (sem chave -> usa score heuristico)'}",
        f"Camada 2 Claude   : {'ON (' + ANTHROPIC_MODEL + ')' if camada2_configurada() else 'OFF (opcional)'}",
    ]
    return "\n".join(linhas)


# Permite testar rapidamente:  python config.py
if __name__ == "__main__":
    print("=== Configuracao carregada ===")
    print(resumo())
