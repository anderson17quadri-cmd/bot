"""
ai_layer1.py  -  CAMADA 1 (Groq principal + DeepSeek fallback)
====================================
Analise PRIMARIA. Corre para TODOS os tokens detetados. Deve ser rapida e
barata. Recebe os dados on-chain e devolve {"score": int, "justificacao": str}.

Tenta primeiro a Groq (latencia muito baixa); se falhar por qualquer razao
(erro, rate limit, timeout, sem chave), cai para a DeepSeek. So levanta
RuntimeError se AMBAS falharem (ou nenhuma estiver configurada).

Chamamos os dois endpoints diretamente com 'requests' (em vez do cliente
'openai') - mais leve, evita problemas de compilacao, e da-nos controlo
total sobre como lemos a resposta (importante porque modelos como o
deepseek-v4-pro podem incluir um campo extra 'reasoning_content' com o
raciocinio interno, separado do 'content' final que queremos).

Funcao principal (mesma assinatura da Camada 2):
    analisar_token(dados_token) -> {"score": int, "justificacao": str}
"""

import requests

import config
import ai_utils

SYSTEM_PROMPT = (
    "Es um analista de risco de tokens na blockchain Solana. "
    "Avalias tokens recem-lancados a procura de sinais de scam/rug pull. "
    "Respondes SEMPRE e APENAS com um objeto JSON valido, sem preambulo nem "
    "texto extra, no formato {\"score\": <0-100>, \"justificacao\": <string>, "
    "\"confianca\": <0-100>}. Score: 0 = seguro, 100 = claramente perigoso/scam. "
    "Confianca: 0-100, o quao confiante estas na tua propria avaliacao "
    "(dados completos = alta; muitos campos em falta/desconhecidos = baixa)."
)


def esta_configurada() -> bool:
    """True se houver chave de algum dos dois providers (Groq ou
    DeepSeek) no .env."""
    return config.camada1_configurada()


def groq_configurado() -> bool:
    """True se houver chave da Groq no .env."""
    return config.groq_configurado()


def _chamar_groq(dados_token: dict) -> dict:
    """Envia os dados a Groq (endpoint compativel com OpenAI) e devolve
    {"score": int, "justificacao": str}. Levanta RuntimeError em falha."""
    if not groq_configurado():
        raise RuntimeError("Groq sem chave de API configurada.")

    prompt_utilizador = ai_utils.construir_prompt(dados_token)

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_utilizador},
        ],
        "temperature": 0.2,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }

    try:
        resposta = requests.post(url, headers=headers, json=payload, timeout=45)
    except Exception as e:
        raise RuntimeError(f"Falha na chamada a Groq (rede/timeout): {e}") from e

    # Status HTTP SEMPRE no log, antes de parsear - para o bot.log mostrar
    # de imediato se e rate limit (429), auth (401), creditos, etc.
    print(f"[Camada 1] Groq HTTP {resposta.status_code}")
    if resposta.status_code != 200:
        # O corpo e onde a Groq explica o erro (rate limit, quota, auth) -
        # nunca o descartar (era o que o raise_for_status fazia)
        raise RuntimeError(
            f"Groq devolveu HTTP {resposta.status_code}: {resposta.text[:300]}")

    # Parsing em try proprio: HTTP 200 com corpo inesperado (ex:
    # {"error": ...}) dava KeyError cuja mensagem era so "'choices'" -
    # agora o corpo bruto vai inteiro (truncado) para a mensagem de erro
    try:
        dados = resposta.json()
        texto = dados["choices"][0]["message"].get("content") or ""
    except Exception as e:
        raise RuntimeError(
            f"Resposta da Groq com formato inesperado ({e!r}). "
            f"Corpo: {resposta.text[:300]}") from e

    if not texto.strip():
        raise RuntimeError(f"Groq devolveu 'content' vazio. Corpo: {resposta.text[:300]}")

    obj = ai_utils.extrair_json(texto)
    return ai_utils.normalizar_resultado(obj)


def _chamar_deepseek(dados_token: dict) -> dict:
    """Envia os dados a DeepSeek e devolve {"score": int, "justificacao": str}.

    Levanta RuntimeError se a camada nao estiver configurada ou a API falhar.
    """
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("Camada 1 (DeepSeek) sem chave de API configurada.")

    prompt_utilizador = ai_utils.construir_prompt(dados_token)

    url = f"{config.DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_utilizador},
        ],
        "temperature": 0.2,
        "max_tokens": 2000,  # subido: modelos com raciocinio podem precisar de mais
        "response_format": {"type": "json_object"},
    }

    try:
        resposta = requests.post(url, headers=headers, json=payload, timeout=45)
    except Exception as e:
        raise RuntimeError(f"Falha na chamada a DeepSeek (rede/timeout): {e}") from e

    # Mesmo padrao da Groq: status sempre no log, corpo nunca descartado
    print(f"[Camada 1] DeepSeek HTTP {resposta.status_code}")
    if resposta.status_code != 200:
        raise RuntimeError(
            f"DeepSeek devolveu HTTP {resposta.status_code}: {resposta.text[:300]}")

    try:
        dados = resposta.json()
        mensagem = dados["choices"][0]["message"]
        # 'content' e sempre o campo com a resposta final (JSON pedido).
        # 'reasoning_content', se existir, e so o raciocinio interno do
        # modelo - nunca o usamos para o parsing.
        texto = mensagem.get("content") or ""
    except Exception as e:
        raise RuntimeError(
            f"Resposta da DeepSeek com formato inesperado ({e!r}). "
            f"Corpo: {resposta.text[:300]}") from e

    if not texto.strip():
        raise RuntimeError(
            "DeepSeek devolveu 'content' vazio (possivel corte por "
            f"max_tokens durante o raciocinio interno). Corpo: {resposta.text[:300]}"
        )

    obj = ai_utils.extrair_json(texto)
    return ai_utils.normalizar_resultado(obj)


def analisar_token(dados_token: dict) -> dict:
    """Tenta a Groq primeiro (mais rapida); se falhar, cai para a DeepSeek.

    So levanta RuntimeError se AMBAS falharem (ou nenhuma estiver
    configurada) - o main.py apanha esse erro e continua com o score
    heuristico, tal como hoje.
    """
    erro_groq = None
    if groq_configurado():
        try:
            resultado = _chamar_groq(dados_token)
            resultado["provider"] = "groq"
            print(f"[Camada 1] provider usado: Groq ({config.GROQ_MODEL})")
            return resultado
        except Exception as e:
            erro_groq = e
            print(f"[Camada 1] Groq falhou ({e}) - a tentar DeepSeek...")

    try:
        resultado = _chamar_deepseek(dados_token)
        resultado["provider"] = "deepseek"
        print(f"[Camada 1] provider usado: DeepSeek ({config.DEEPSEEK_MODEL})")
        return resultado
    except Exception as e:
        if erro_groq is not None:
            raise RuntimeError(
                f"Groq e DeepSeek falharam. Groq: {erro_groq}. DeepSeek: {e}"
            ) from e
        raise


# --------------------------------------------------------------------------
# Teste rapido:  python ai_layer1.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    exemplo = {
        "token_simbolo": "TESTE", "token_mint": "5LA7...pump", "dex": "pump-fun",
        "nome_par": "TESTE / SOL", "liquidez_usd": 1900, "fdv_usd": 2400,
        "idade_minutos": 1.1, "onchain_disponivel": True,
        "mint_authority": None, "freeze_authority": None, "supply": 1_000_000_000,
        "holders_disponivel": False, "top_holder_pct": 0.0, "score_heuristico": 20,
        "fatores_risco": ["Mint authority revogada (bom sinal)", "Liquidez baixa: $1,900"],
    }

    if not esta_configurada():
        print("Camada 1 NAO configurada (sem DEEPSEEK_API_KEY no .env).")
    else:
        print("A chamar a DeepSeek com um token de exemplo...\n")
        for i in range(3):
            resultado = analisar_token(exemplo)
            print(f"Tentativa {i+1}:", resultado)
