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
    "liquidez_bloqueada", "deployer_tokens_criados", "liquidez_suspeita",
    # Campos especificos de BSC (so aparecem quando chain=="bsc")
    "chain", "honeypot", "buy_tax", "sell_tax",
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
        "Significado dos sinais:\n"
        "- 'mint_authority' nao-nulo = a equipa pode imprimir mais tokens (red flag).\n"
        "- 'freeze_authority' nao-nulo = podem congelar carteiras (red flag grave).\n"
        "- Liquidez muito baixa aumenta o risco de rug/pouca saida.\n"
        "- 'top_holder_pct' = % do supply na maior conta; 'top5_holders_pct' = % nas 5\n"
        "  maiores JUNTAS. Concentracao alta no top 5 e red flag mesmo que nenhuma\n"
        "  conta sozinha domine (ex: 5 carteiras com 15% cada). Lembra-te que o maior\n"
        "  holder pode ser o proprio pool de liquidez (desconta-o na tua leitura).\n"
        "- 'liquidez_bloqueada': 'queimada' ou 'bloqueada_protocolo' = o criador NAO\n"
        "  consegue retirar a liquidez (sinal MUITO forte de seguranca);\n"
        "  'nao_bloqueada' = pode retirar a qualquer momento (red flag forte de rug);\n"
        "  'desconhecido' = sem dados, nao penalizes.\n"
        "- 'deployer_tokens_criados': quantos tokens o criador lancou nas ultimas 48h.\n"
        "  Mais de 5 = padrao de scam em serie. null = sem dados, nao penalizes.\n"
        "- 'liquidez_suspeita' true = liquidez ja muito alta para a idade do pool\n"
        "  (possivel inflacao artificial). E so um indicio, pesa-o com moderacao.\n\n"
        "Como decidir (avaliacao HOLISTICA, nao mecanica):\n"
        "- Pondera os sinais em conjunto: NAO penalizes automaticamente so por um\n"
        "  sinal fraco isolado se os restantes forem muito fortes (ex: LP queimado +\n"
        "  autoridades revogadas + holders distribuidos compensam liquidez mediana).\n"
        "- O inverso tambem vale: um unico sinal gravissimo (freeze authority ativa,\n"
        "  LP nao bloqueado) pode justificar score alto mesmo com o resto positivo.\n"
        "- Na 'justificacao', diz QUAL foi o sinal decisivo para o teu score.\n"
        "- Dados em falta nao sao red flags - refletem-se na 'confianca', nao no score.\n\n"
        "Devolve APENAS um objeto JSON valido, sem qualquer texto extra, no formato:\n"
        '{"score": <inteiro 0-100, onde 0=seguro e 100=perigoso/scam>, '
        '"justificacao": "<frase curta, max 200 caracteres, em portugues, '
        'a nomear o sinal decisivo>", '
        '"confianca": <inteiro 0-100: confianca na tua propria avaliacao - '
        'dados completos = alta; muitos campos em falta/desconhecidos = baixa>}'
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
    """Garante que devolvemos SEMPRE:
      {"score": int(0-100), "justificacao": str, "confianca": int(0-100)}

    'confianca' = o quao confiante o modelo esta na propria avaliacao
    (dados completos = alta; muitos dados em falta = baixa). Ajuda a
    distinguir "score alto porque e mesmo arriscado" de "score alto
    porque faltam dados". Se o modelo nao a devolver, assumimos 50.

    Se algo vier mal formatado, usamos valores seguros por defeito em vez
    de rebentar. (Melhor um alerta com aviso do que o bot a morrer.)
    """
    if not isinstance(obj, dict):
        return {
            "score": 50,
            "justificacao": "Resposta da IA ilegivel (formato inesperado).",
            "confianca": 50,
        }

    def _int_0_100(valor, defeito: int) -> int:
        """Converte para inteiro no intervalo 0-100 (defeito se vier mal)."""
        try:
            numero = int(round(float(valor)))
        except (TypeError, ValueError):
            return defeito
        return max(0, min(100, numero))

    score = _int_0_100(obj.get("score", 50), 50)
    confianca = _int_0_100(obj.get("confianca", 50), 50)

    # Justificacao -> texto (cortado se for enorme)
    justificacao = obj.get("justificacao") or obj.get("justification") or "Sem justificacao."
    justificacao = str(justificacao).strip()
    if len(justificacao) > 300:
        justificacao = justificacao[:297] + "..."

    return {"score": score, "justificacao": justificacao, "confianca": confianca}


# --------------------------------------------------------------------------
# Teste rapido:  python ai_utils.py
# Testa o parser de JSON com varios casos (limpo, com lixo a volta, invalido).
# --------------------------------------------------------------------------
if __name__ == "__main__":
    casos = [
        '{"score": 30, "justificacao": "ok", "confianca": 85}',      # completo
        '{"score": 30, "justificacao": "ok"}',                       # sem confianca -> 50
        'Claro! Aqui esta:\n{"score": 80, "justificacao": "risco"} ',  # com preambulo
        '```json\n{"score": 12, "justificacao": "seguro"}\n```',      # em bloco markdown
        'nao e json nenhum',                                          # invalido
        '{"score": "150", "justificacao": "", "confianca": -10}',     # fora do range
    ]
    for c in casos:
        obj = extrair_json(c)
        print(f"entrada : {c!r}")
        print(f"parsed  : {obj}")
        print(f"normal. : {normalizar_resultado(obj)}\n")
