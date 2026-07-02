"""
ai_layer2.py  -  CAMADA 2 (Claude / Sonnet)
============================================
Segunda validacao, MAIS CUIDADOSA. So corre quando o score da Camada 1 cai
na "zona ambigua" (definida no config/.env, ex: 40-70). Serve para decidir
melhor os casos que nao sao nem claramente seguros nem claramente scam.

IMPORTANTE: e OPCIONAL. Se nao houver ANTHROPIC_API_KEY no .env, o bot corre
so com a Camada 1 (esta camada e simplesmente ignorada, sem rebentar).

Mesma assinatura da Camada 1:
    analisar_token(dados_token) -> {"score": int, "justificacao": str}
"""

import anthropic

import config
import ai_utils

# Instrucoes ao Claude. Aqui pedimos uma analise mais ponderada, mas na mesma
# so JSON como saida (para o parsing automatico funcionar).
SYSTEM_PROMPT = (
    "Es um analista senior de risco de tokens Solana. Estas a fazer uma "
    "SEGUNDA validacao, mais cuidadosa, de um token cujo risco ficou ambiguo "
    "na primeira analise. Pondera bem os sinais contraditorios. "
    "Responde APENAS com um objeto JSON valido, sem texto a volta, no formato "
    "{\"score\": <0-100>, \"justificacao\": <string>}. "
    "Score: 0 = seguro, 100 = claramente perigoso/scam."
)

_cliente = None


def esta_configurada() -> bool:
    """True se houver chave do Claude (Anthropic) no .env."""
    return config.camada2_configurada()


def _obter_cliente() -> "anthropic.Anthropic":
    """Cria (uma vez) e devolve o cliente da API Anthropic (Claude)."""
    global _cliente
    if _cliente is None:
        _cliente = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _cliente


def analisar_token(dados_token: dict) -> dict:
    """Envia os dados ao Claude e devolve {"score": int, "justificacao": str}.

    Levanta RuntimeError se nao estiver configurada ou a API falhar.
    (O main.py apanha o erro e mantem o resultado da Camada 1.)
    """
    if not esta_configurada():
        raise RuntimeError("Camada 2 (Claude) sem chave de API configurada.")

    cliente = _obter_cliente()
    prompt_utilizador = ai_utils.construir_prompt(dados_token)

    try:
        resposta = cliente.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=400,
            temperature=0.2,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt_utilizador}],
        )
    except Exception as e:
        raise RuntimeError(f"Falha na chamada ao Claude: {e}") from e

    # A resposta do Claude vem numa lista de "blocos"; juntamos o texto todo.
    texto = "".join(bloco.text for bloco in resposta.content if hasattr(bloco, "text"))
    obj = ai_utils.extrair_json(texto)
    return ai_utils.normalizar_resultado(obj)


# --------------------------------------------------------------------------
# Teste rapido:  python ai_layer2.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    exemplo = {
        "token_simbolo": "TESTE", "token_mint": "5LA7...pump", "dex": "raydium",
        "nome_par": "TESTE / SOL", "liquidez_usd": 3500, "fdv_usd": 40000,
        "idade_minutos": 0.6, "onchain_disponivel": True,
        "mint_authority": None, "freeze_authority": None, "supply": 1_000_000_000,
        "holders_disponivel": False, "top_holder_pct": 0.0, "score_heuristico": 30,
        "fatores_risco": ["Mint authority revogada (bom sinal)", "Liquidez media: $3,500"],
    }

    if not esta_configurada():
        print("Camada 2 NAO configurada (sem ANTHROPIC_API_KEY no .env).")
        print("-> Isto e OPCIONAL: o bot funciona so com a Camada 1.")
    else:
        print("A chamar o Claude com um token de exemplo...\n")
        resultado = analisar_token(exemplo)
        print("Resultado:", resultado)
