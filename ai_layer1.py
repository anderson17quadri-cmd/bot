"""
ai_layer1.py  -  CAMADA 1 (DeepSeek)
====================================
Analise PRIMARIA. Corre para TODOS os tokens detetados. Deve ser rapida e
barata. Recebe os dados on-chain e devolve {"score": int, "justificacao": str}.

A DeepSeek e compativel com a API da OpenAI, por isso usamos o cliente
'openai' apontado para o endpoint da DeepSeek (definido no config/.env).

Funcao principal (mesma assinatura da Camada 2):
    analisar_token(dados_token) -> {"score": int, "justificacao": str}
"""

from openai import OpenAI

import config
import ai_utils

# Instrucoes fixas ao modelo. Pedimos JSON e SO JSON.
SYSTEM_PROMPT = (
    "Es um analista de risco de tokens na blockchain Solana. "
    "Avalias tokens recem-lancados a procura de sinais de scam/rug pull. "
    "Respondes SEMPRE e APENAS com um objeto JSON valido, sem preambulo nem "
    "texto extra, no formato {\"score\": <0-100>, \"justificacao\": <string>}. "
    "Score: 0 = seguro, 100 = claramente perigoso/scam."
)

# Guardamos o cliente numa variavel de modulo para nao o recriar a cada chamada.
_cliente = None


def esta_configurada() -> bool:
    """True se houver chave da DeepSeek no .env."""
    return config.camada1_configurada()


def _obter_cliente() -> OpenAI:
    """Cria (uma vez) e devolve o cliente da API DeepSeek."""
    global _cliente
    if _cliente is None:
        _cliente = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
        )
    return _cliente


def analisar_token(dados_token: dict) -> dict:
    """Envia os dados a DeepSeek e devolve {"score": int, "justificacao": str}.

    Levanta RuntimeError se a camada nao estiver configurada ou a API falhar.
    (O main.py apanha esse erro e continua com o score heuristico.)
    """
    if not esta_configurada():
        raise RuntimeError("Camada 1 (DeepSeek) sem chave de API configurada.")

    cliente = _obter_cliente()
    prompt_utilizador = ai_utils.construir_prompt(dados_token)

    try:
        resposta = cliente.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt_utilizador},
            ],
            temperature=0.2,          # baixa = respostas mais consistentes
            max_tokens=300,
            # Modo JSON: pede a API para garantir saida JSON valida
            response_format={"type": "json_object"},
        )
    except Exception as e:
        # Rede, chave invalida, modelo errado, etc.
        raise RuntimeError(f"Falha na chamada a DeepSeek: {e}") from e

    texto = resposta.choices[0].message.content
    obj = ai_utils.extrair_json(texto)
    return ai_utils.normalizar_resultado(obj)


# --------------------------------------------------------------------------
# Teste rapido:  python ai_layer1.py
# Se nao houver chave, mostra que "salta" com elegancia (nao rebenta).
# Se houver chave, faz uma chamada real com um token de exemplo.
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
        print("-> Em producao, o main.py usaria o score heuristico neste caso.")
    else:
        print("A chamar a DeepSeek com um token de exemplo...\n")
        resultado = analisar_token(exemplo)
        print("Resultado:", resultado)
