"""
ai_utils.py
===========
Funcoes partilhadas pelas duas camadas de IA (ai_layer1 e ai_layer2):

  - construir_prompt(dados_token) -> o texto que enviamos ao modelo
  - extrair_json(texto)           -> le a resposta e apanha o JSON com seguranca
  - normalizar_resultado(obj)     -> garante {"score": int 0-100, "justificacao": str}

Assim nao repetimos codigo e mantemos o comportamento consistente entre as
duas camadas.
"""

import json


# Lista de campos do "dados_token" que faz sentido enviar a IA.
# (Enviamos so o util, para o prompt ficar curto e barato.)
_CAMPOS_PARA_IA = [
    "token_simbolo", "token_mint", "dex", "nome_par",
    "liquidez_usd", "fdv_usd", "idade_minutos",
    "onchain_disponivel", "mint_authority", "freeze_authority", "supply",
    "holders_disponivel", "top_holder_pct", "top5_holders_pct",
    "score_heuristico", "fatores_risco",
]


def construir_prompt(dados_token: dict) -> str:
    """Cria o texto (mensagem do utilizador) a enviar ao modelo.

    Inclui os dados on-chain em JSON + avisos importantes para a IA nao
    tirar conclusoes erradas (ex: maior holder pode ser o pool de liquidez).
    """
    # Filtrar so os campos uteis
    resumo = {k: dados_token.get(k) for k in _CAMPOS_PARA_IA}
    dados_json = json.dumps(resumo, ensure_ascii=False, indent=2)

    prompt = (
        "Analisa o risco deste token recem-lancado na Solana.\n"
        "Dados on-chain e de mercado (em JSON):\n"
        f"{dados_json}\n\n"
        "Regras de interpretacao:\n"
        "- 'mint_authority' nao-nulo = a equipa pode imprimir mais tokens (red flag).\n"
        "- 'freeze_authority' nao-nulo = podem congelar carteiras (red flag grave).\n"
        "- Liquidez muito baixa aumenta o risco de rug/pouca saida.\n"
        "- Se 'holders_disponivel' for false, NAO penalizes por isso; menciona a incerteza.\n"
        "- 'top_holder_pct' = % do supply na maior conta; 'top5_holders_pct' = % nas 5\n"
        "  maiores JUNTAS. Concentracao alta no top 5 e red flag mesmo que nenhuma\n"
        "  conta sozinha domine (ex: 5 carteiras com 15% cada).\n"
        "- Se olhares a concentracao, lembra-te que o maior holder pode ser o proprio\n"
        "  pool de liquidez (nesse caso desconta essa conta na tua leitura).\n\n"
        "Devolve APENAS um objeto JSON valido, sem qualquer texto extra, no formato:\n"
        '{"score": <inteiro 0-100, onde 0=seguro e 100=perigoso/scam>, '
        '"justificacao": "<frase curta, max 200 caracteres, em portugues>"}'
    )
    return prompt


def extrair_json(texto: str) -> dict | None:
    """Le a resposta do modelo e tenta extrair o objeto JSON.

    Estrategia defensiva (os modelos as vezes metem texto a volta):
      1) tentar ler o texto todo como JSON
      2) se falhar, apanhar do primeiro '{' ao ultimo '}' e tentar de novo
    Devolve o dicionario, ou None se nao conseguir.
    """
    if not texto:
        return None

    texto = texto.strip()

    # Tentativa 1: o texto ja e JSON puro
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass

    # Tentativa 2: extrair o bloco entre { ... }
    inicio = texto.find("{")
    fim = texto.rfind("}")
    if inicio != -1 and fim != -1 and fim > inicio:
        try:
            return json.loads(texto[inicio:fim + 1])
        except json.JSONDecodeError:
            return None

    return None


def normalizar_resultado(obj: dict | None) -> dict:
    """Garante que devolvemos SEMPRE {"score": int(0-100), "justificacao": str}.

    Se algo vier mal formatado, usamos valores seguros por defeito em vez
    de rebentar. (Melhor um alerta com aviso do que o bot a morrer.)
    """
    if not isinstance(obj, dict):
        return {"score": 50, "justificacao": "Resposta da IA ilegivel (formato inesperado)."}

    # Score -> inteiro entre 0 e 100
    try:
        score = int(round(float(obj.get("score", 50))))
    except (TypeError, ValueError):
        score = 50
    score = max(0, min(100, score))

    # Justificacao -> texto (cortado se for enorme)
    justificacao = obj.get("justificacao") or obj.get("justification") or "Sem justificacao."
    justificacao = str(justificacao).strip()
    if len(justificacao) > 300:
        justificacao = justificacao[:297] + "..."

    return {"score": score, "justificacao": justificacao}


# --------------------------------------------------------------------------
# Teste rapido:  python ai_utils.py
# Testa o parser de JSON com varios casos (limpo, com lixo a volta, invalido).
# --------------------------------------------------------------------------
if __name__ == "__main__":
    casos = [
        '{"score": 30, "justificacao": "ok"}',                       # limpo
        'Claro! Aqui esta:\n{"score": 80, "justificacao": "risco"} ',  # com preambulo
        '```json\n{"score": 12, "justificacao": "seguro"}\n```',      # em bloco markdown
        'nao e json nenhum',                                          # invalido
        '{"score": "150", "justificacao": ""}',                       # score fora do range
    ]
    for c in casos:
        obj = extrair_json(c)
        print(f"entrada : {c!r}")
        print(f"parsed  : {obj}")
        print(f"normal. : {normalizar_resultado(obj)}\n")
