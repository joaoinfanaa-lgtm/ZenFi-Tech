# -*- coding: utf-8 -*-
"""
ZenFi - interpretação de mensagens escritas em português (assistente do WhatsApp).

    interpretar("Gastei R$ 35 no almoço")
    -> {"tipo": "despesa", "centavos": 3500, "descricao": "Almoço",
        "categoria": "Alimentação", "data": date(...), "forcar": False}

    interpretar_varios("gastei 30 no lanche e 20 no uber ontem")  -> 2 lançamentos
    comando("resumo do mês passado")                               -> ("resumo", {"mes": "passado"})
    motivo_falha("gastei 10 dólares")                              -> "moeda"

Só usa a biblioteca padrão. Devolve None / lista vazia quando não entende a
mensagem (sem valor, sem como saber se é receita ou despesa, valor absurdo…):
nesse caso o servidor NÃO lança nada e explica o que faltou.
"""
import difflib
import re
import unicodedata
from datetime import date, timedelta

# Categorias que existem no app (CATS do HTML). "Outros" é o padrão.
CATEGORIAS = [
    "Moradia", "Alimentação", "Transporte", "Saúde", "Educação", "Lazer",
    "Contas e serviços", "Dízimo", "Oferta", "Seguro", "Escola", "Mercado",
    "Internet fixa", "Internet móvel", "Feira", "Padaria", "Meus Cuidados",
    "Outros",
]

MAX_CENTAVOS = 10_000_000_00  # R$ 10 milhões por lançamento
MAX_ITENS = 8                 # lançamentos por mensagem

# Verbos que dizem com clareza o que aconteceu: o primeiro que aparece vence.
VERBOS_RECEITA = (
    "recebi", "ganhei", "vendi", "faturei", "entrou", "caiu", "depositaram", "recebido", "recebeu",
    "recebemos", "me pagaram", "me pagou", "me devolveram", "me devolveu", "me transferiram",
    "me mandaram", "me enviaram", "me deram", "estornaram", "reembolsaram",
)
VERBOS_DESPESA = (
    "gastei", "paguei", "comprei", "debitou", "saiu", "transferi", "doei", "pagamos", "gastamos",
    "mandei", "enviei", "fiz um pix", "fiz uma transferencia", "emprestei", "devolvi", "abasteci",
    "recarreguei", "assinei", "renovei", "adquiri", "almocei", "jantei", "lanchei", "pagou",
    "comprou", "pagamento",
)
# Substantivos: só valem quando não há verbo ("salário 3000", "uber 18,90").
SUBST_RECEITA = (
    "salario", "recebimento", "freela", "freelance", "rendimento", "rendeu", "reembolso",
    "restituicao", "deposito", "comissao", "bonus", "13o", "decimo terceiro", "estorno",
    "cashback", "aluguel recebido", "pro labore", "prolabore", "mesada", "venda", "vendas",
    "dividendos", "lucro",
)
SUBST_DESPESA = (
    "gasto", "gastos", "pago", "compra", "despesa", "boleto", "parcela", "fatura",
    "assinatura", "pix para", "pix pro", "pix pra",
)

# (regex sobre o texto normalizado SEM o valor/data, categoria) - a primeira que casar vence.
REGRAS_CATEGORIA = [
    (r"\bdizimo", "Dízimo"),
    (r"\bofertas?\b", "Oferta"),
    (r"\bpadaria|\bpao\b|\bpaes\b|\bpao de queijo|\bsalgado|\bcoxinha|\bpastel\b", "Padaria"),
    (r"\bfeira\b|\bhortifruti|\bsacolao|\bverdura|\blegume|\bfruta", "Feira"),
    (r"\bsupermercado|\bmercado\b(?! livre| pago)|\bmercadinho|\batacadao|\bcompras do mes|\bacougue|\bmercearia|\bassai\b|\bcarrefour", "Mercado"),
    (r"\balmoco|\bjantar|\bjanta\b|\blanche|\blanchonete|\brestaurante|\bifood|\brappi|\buber eats|\bcafe\b|\bpizza|\bhamburguer|\bburger|\bsorvete|\bacai\b|\bsushi|\bchurrasco|\bmarmita|\bbar\b|\bcerveja|\bchopp|\bcafeteria|\bcomida|\bcafe da manha|\bmcdonald|\bsubway|\bpetisco|\bdoce\b|\bbombom|\bchocolate", "Alimentação"),
    (r"\bgasolina|\bcombustivel|\betanol|\bdiesel|\bposto\b|\buber\b|\b99 ?pop|\b99 ?taxi|\bcabify|\btaxi|\bonibus|\bmetro\b|\btrem\b|\bpassagem|\bbilhete|\bestacionamento|\bpedagio|\bsem parar|\bmecanico|\boficina|\bipva|\bpneu|\blava jato|\blavagem|\bcarro\b|\bmoto\b|\bcorrida\b", "Transporte"),
    (r"\bfarmacia|\bdrogaria|\bremedio|\bmedico|\bconsulta|\bdentista|\bexame|\bhospital|\bplano de saude|\bsaude\b|\bterapia|\bpsicolog|\blaboratorio|\bvacina|\boculos|\boptica", "Saúde"),
    (r"\bescola\b|\bcreche|\bcolegio|\bmaterial escolar|\buniforme", "Escola"),
    (r"\bsalao|\bbarbeiro|\bbarbearia|\bcabeleireir|\bcabelo|\bunhas?\b|\bmanicure|\bcosmetico|\bperfume|\bacademia|\bskincare|\bmaquiagem|\bdepilacao|\bmassagem|\bpilates|\bcrossfit", "Meus Cuidados"),
    (r"\bcurso|\bfaculdade|\buniversidade|\blivro|\bapostila|\bidioma|\bingles\b|\bespanhol\b", "Educação"),
    (r"\bcinema|\bshow\b|\bviagem|\bhotel|\bpousada|\bnetflix|\bspotify|\bdisney|\bamazon prime|\bprime video|\bhbo|\bmax\b|\byoutube|\bdeezer|\bstreaming|\bjogo|\bgame|\bplaystation|\bsteam\b|\bpasseio|\bparque|\bfesta|\bbalada|\bteatro|\bingresso|\blazer", "Lazer"),
    (r"\binternet|\bwifi|\bwi-fi|\bfibra|\bbanda larga", "Internet fixa"),
    (r"\bcelular|\brecarga|\bplano de celular|\bdados moveis|\b4g\b|\b5g\b|\bchip\b", "Internet móvel"),
    (r"\bseguro", "Seguro"),
    (r"\baluguel|\bcondominio|\biptu|\bfinanciamento|\breforma|\bmoveis|\bmovel\b|\bmorad", "Moradia"),
    (r"\bluz\b|\benergia|\bagua\b|\bgas\b|\btelefone|\besgoto|\btv a cabo|\bboleto|\bfatura\b", "Contas e serviços"),
    (r"\bmensalidade|\bmatricula", "Educação"),
]

# Palavras que não dizem o que foi comprado: saem das pontas da descrição.
PALAVRAS_RUIDO = {
    "reais", "real", "conto", "contos", "pila", "pilas", "gastei", "gasto", "gastos", "paguei", "pagamento",
    "pago", "comprei", "compra", "recebi", "recebimento", "ganhei", "entrou", "caiu", "vendi",
    "de", "do", "da", "dos", "das", "no", "na", "nos", "nas", "em", "com", "um", "uma", "o", "a",
    "os", "as", "foi", "foram", "hoje", "ontem", "anteontem", "dia", "para", "pra", "por", "pelo",
    "pela", "pro", "r", "rs", "meu", "minha", "me", "conta", "lancar", "lancei", "lance", "registrar",
    "registre", "despesa", "debitou", "saiu", "transferi", "e", "que", "ao", "aos", "mais", "novo",
    "fiz", "mandei", "enviei", "abasteci", "doei", "emprestei", "devolvi", "ate", "so", "cerca",
    "aproximadamente", "devolveram", "devolveu", "pagaram", "pagou", "transferiram", "mandaram", "enviaram", "deram", "estornaram", "mesmo", "assim", "total", "valor", "novamente", "agora", "ja", "tambem",
}
_PREPOSICOES = {"de", "do", "da", "dos", "das", "no", "na", "nos", "nas", "em", "com", "pro", "pra",
                "para", "por", "pelo", "pela", "ao", "aos", "a", "o", "as", "os"}

_DIAS_SEMANA = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6}

# ------------------------------------------------------------------ números por extenso
_UNID = {"zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5,
         "seis": 6, "sete": 7, "oito": 8, "nove": 9}
_ONZE = {"dez": 10, "onze": 11, "doze": 12, "treze": 13, "catorze": 14, "quatorze": 14, "quinze": 15,
         "dezesseis": 16, "dezessete": 17, "dezoito": 18, "dezenove": 19}
_DEZ = {"vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50, "sessenta": 60, "setenta": 70,
        "oitenta": 80, "noventa": 90}
_CEM = {"cem": 100, "cento": 100, "duzentos": 200, "trezentos": 300, "quatrocentos": 400,
        "quinhentos": 500, "seiscentos": 600, "setecentos": 700, "oitocentos": 800, "novecentos": 900}
_PALAVRAS_NUM = set(_UNID) | set(_ONZE) | set(_DEZ) | set(_CEM) | {"mil", "e"}
_MOEDA = r"(?:reais|real|contos?|pilas?)"


def _valor_extenso(palavras):
    """['dois','mil','e','quinhentos'] -> 2500 (None se não for número bem formado)."""
    total, atual, viu = 0, 0, False
    for p in palavras:
        if p == "e":
            continue
        if p == "mil":
            total += (atual or 1) * 1000
            atual, viu = 0, True
        elif p in _CEM:
            atual += _CEM[p]; viu = True
        elif p in _DEZ:
            atual += _DEZ[p]; viu = True
        elif p in _ONZE:
            atual += _ONZE[p]; viu = True
        elif p in _UNID:
            atual += _UNID[p]; viu = True
        else:
            return None
    return total + atual if viu else None


def _numeros_por_extenso(original):
    """'cinquenta e cinco reais' -> '55 reais' (só quando é dinheiro); o resto do texto fica como veio."""
    otoks = original.split(" ")
    ntoks = [re.sub(r"[,.!?;]+$", "", _sem_acento(x).lower()) for x in otoks]
    saida, i = [], 0
    while i < len(ntoks):
        if ntoks[i] in _PALAVRAS_NUM and ntoks[i] != "e":
            j = i
            while j < len(ntoks) and ntoks[j] in _PALAVRAS_NUM:
                j += 1
            while j > i and ntoks[j - 1] == "e":
                j -= 1
            trecho = ntoks[i:j]
            v = _valor_extenso(trecho)
            dinheiro = j < len(ntoks) and bool(re.fullmatch(_MOEDA, ntoks[j]))
            depois_de_numero = trecho[0] == "mil" and i > 0 and re.fullmatch(r"[\d.,]+", ntoks[i - 1])
            # sem "reais" só troca números "de dinheiro" (>= 10); '2 mil' fica como está
            if depois_de_numero and saida and re.fullmatch(r"\d+", saida[-1]) and len(trecho) > 1:
                resto = _valor_extenso(trecho[1:])      # "2 mil e quinhentos" -> 2500
                if resto is not None:
                    saida[-1] = str(int(saida[-1]) * 1000 + resto); i = j; continue
            if v is not None and not depois_de_numero and (dinheiro or v >= 10):
                saida.append(str(v)); i = j; continue
        saida.append(otoks[i]); i += 1
    return " ".join(saida)


# ------------------------------------------------------------------ utilidades
def _sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalizar(s):
    return re.sub(r"\s+", " ", _sem_acento(str(s or "")).lower()).strip()


def _para_centavos(bruto):
    """'2.500,50' -> 250050 ; '35' -> 3500 ; '1.200' -> 120000 ; '35.5' -> 3550."""
    s = bruto.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif re.fullmatch(r"\d{1,3}\.\d{3}", s):
        s = s.replace(".", "")
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return None


_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_MULT = r"(?:\s*(mil|k)\b)?"


def _pre_normalizar(original):
    """Ajustes de escrita antes de analisar (mantém o texto legível para a descrição)."""
    s = re.sub(r"(\d+)\s*(?:reais|real)\s*e\s*(\d{1,2})\s*centavos?\b", r"\1,\2 reais", original, flags=re.I)
    s = re.sub(r"r\$\s*", "R$ ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def _valores(t):
    """Valores em dinheiro de um texto normalizado: [(centavos, (ini, fim))] em ordem."""
    achados, ocupado = [], []

    def livre(a, b):
        return all(b <= x or a >= y for x, y in ocupado)

    def add(m):
        if not livre(m.start(), m.end()):
            return
        c = _para_centavos(m.group(1))
        if c is None:
            return
        if m.re.groups >= 2 and m.group(2):
            c *= 1000
        achados.append((c, m.span()))
        ocupado.append(m.span())

    # datas, "dia N", parcelas, horas e prazos não são dinheiro: bloqueia esses trechos
    for rx in (r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", r"\bdia\s+\d{1,2}\b",
               r"\bparcelas?\s+\d{1,2}\s*(?:/|de)\s*\d{1,2}\b", r"\b\d{1,2}\s*(?:/|de)\s*\d{1,2}\s*parcelas?\b",
               r"\b\d{1,2}\s*x\b", r"\b\d{1,2}\s*(?:h|hs|hrs)\b", r"\b\d{1,2}:\d{2}\b", r"(?<![\d.,])\d{1,2}\s*dias\b", r"(?<![\d.,])1\s*dia\b"):
        for m in re.finditer(rx, t):
            ocupado.append(m.span())
    for m in re.finditer(r"r\$\s*(" + _NUM + r")" + _MULT, t):                                   # R$ 35
        add(m)
    for m in re.finditer(r"(?<![\w/])(" + _NUM + r")" + _MULT + r"\s*" + _MOEDA + r"\b", t):      # 35 reais
        add(m)
    for m in re.finditer(r"(?<![\w/])(" + _NUM + r")\s*(mil|k)\b", t):                           # 2 mil, 1,5k
        add(m)
    if not achados:
        for m in re.finditer(r"(?<![\w/$])(" + _NUM + r")(?![\w/])", t):                        # 35
            add(m)
    achados.sort(key=lambda x: x[1][0])
    return achados


def _estrangeira(t):
    return bool(re.search(r"\b(dolar|dolares|euro|euros|libra|libras|us\$|usd|eur|bitcoin|btc)\b|(?<![\w])\$\s*\d", t))


# ------------------------------------------------------------------ datas
def _extrair_data(t, hoje):
    """Retorna (data, (ini, fim) do trecho ou None, explícita?). Padrão: hoje."""
    for rx, delta in ((r"\banteontem\b", 2), (r"\bontem\b", 1), (r"\bhoje\b", 0)):
        m = re.search(rx, t)
        if m:
            return hoje - timedelta(days=delta), m.span(), True
    # "sexta", "na sexta passada": o dia da semana mais recente (nunca no futuro)
    m = re.search(r"\b(segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:-feira| feira)?(?: passad[ao])?\b(?! parcela| via| vez| mao| opcao)", t)
    if m:
        alvo = _DIAS_SEMANA[m.group(1)]
        dif = (hoje.weekday() - alvo) % 7
        return hoje - timedelta(days=dif or 7), m.span(), True
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t):
        antes, depois = t[max(0, m.start() - 12):m.start()], t[m.end():m.end() + 10]
        if re.search(r"parcel\w*\s*$|parc\.?\s*$", antes) or re.match(r"\s*parcel", depois):
            continue                                  # "parcela 3/10" não é data
        d, mes = int(m.group(1)), int(m.group(2))
        ano = hoje.year
        if m.group(3):
            ano = int(m.group(3))
            ano += 2000 if ano < 100 else 0
        try:
            dt = date(ano, mes, d)
            if not m.group(3) and dt > hoje + timedelta(days=1):
                dt = date(ano - 1, mes, d)           # "28/12" dito em janeiro = ano passado
        except ValueError:
            continue
        return dt, m.span(), True
    m = re.search(r"\bdia\s+(\d{1,2})\b(?:\s+do\s+mes\s+passado)?", t)
    if m:
        d = int(m.group(1))
        ano, mes = hoje.year, hoje.month
        if d > hoje.day + 1 or "mes passado" in m.group(0):   # "dia 28" dito no dia 5 -> mês passado
            mes -= 1
            if mes == 0:
                mes, ano = 12, ano - 1
        for dd in (d, d - 1, d - 2, d - 3):                   # dia 31 em mês curto
            if dd < 1:
                break
            try:
                return date(ano, mes, dd), m.span(), True
            except ValueError:
                continue
    return hoje, None, False


# ------------------------------------------------------------------ tipo e categoria
def _primeira(t, palavras):
    pos = None
    for k in palavras:
        m = re.search(r"\b" + re.escape(k) + r"\b", t)
        if m and (pos is None or m.start() < pos):
            pos = m.start()
    return pos


def _tipo(t, sinal=None):
    if sinal == "+":
        return "receita"
    if sinal == "-":
        return "despesa"
    vr, vd = _primeira(t, VERBOS_RECEITA), _primeira(t, VERBOS_DESPESA)
    if vr is not None or vd is not None:           # verbo claro: vence o que aparece primeiro
        if vd is None:
            return "receita"
        if vr is None:
            return "despesa"
        return "receita" if vr < vd else "despesa"
    sr, sd = _primeira(t, SUBST_RECEITA), _primeira(t, SUBST_DESPESA)
    if sr is not None and sd is None:
        return "receita"
    if sd is not None and sr is None:
        return "despesa"
    if sr is not None and sd is not None:
        return "receita" if sr <= sd else "despesa"
    return None


def categoria_para(t_normalizado):
    for rx, cat in REGRAS_CATEGORIA:
        if re.search(rx, t_normalizado):
            return cat
    return "Outros"


# Palavras-chave para a busca por semelhança (aceita erro de digitação/fala: "farmasia", "gazolina", "feirinha").
_PALAVRAS_SEMELHANTES = {
    "Moradia": "aluguel condominio iptu financiamento prestacao casa apartamento reforma pedreiro encanador eletricista moveis sofa geladeira fogao colchao faxina diarista moradia",
    "Alimentação": "almoco jantar janta lanche lanchonete restaurante ifood rappi pizza pizzaria hamburguer burger sushi churrasco marmita quentinha comida refeicao cafe cafeteria cerveja chopp bebida sorvete acai doce chocolate bombom delivery buffet alimentacao",
    "Transporte": "uber taxi cabify onibus metro trem passagem bilhete combustivel gasolina etanol alcool diesel posto abasteci abastecimento estacionamento pedagio mecanico oficina ipva licenciamento pneu revisao moto carro lavagem bicicleta multa guincho transporte",
    "Saúde": "farmacia drogaria remedio remedios medicamento medico consulta dentista exame exames hospital terapia psicologo psicologa laboratorio vacina oculos optica fisioterapia cirurgia saude",
    "Educação": "curso faculdade universidade livro livros apostila idioma ingles espanhol matricula treinamento educacao",
    "Lazer": "cinema show teatro viagem hotel pousada netflix spotify disney youtube deezer streaming jogo jogos playstation steam passeio parque festa balada ingresso lazer diversao clube",
    "Contas e serviços": "energia esgoto telefone boleto fatura tarifa cartorio imposto",
    "Dízimo": "dizimo dizimos",
    "Oferta": "oferta ofertas ofertorio doacao",
    "Seguro": "seguro seguros apolice",
    "Escola": "escola creche colegio uniforme mensalidade",
    "Mercado": "mercado supermercado atacado atacadao mercadinho mercearia acougue carne compras",
    "Internet fixa": "internet wifi fibra roteador",
    "Internet móvel": "celular recarga",
    "Feira": "feira hortifruti sacolao verdura verduras legume legumes fruta frutas",
    "Padaria": "padaria paes bolo salgado salgados coxinha pastel",
    "Meus Cuidados": "salao barbeiro barbearia cabeleireiro cabelo unha unhas manicure cosmetico cosmeticos perfume academia skincare maquiagem depilacao massagem pilates crossfit estetica cuidados",
}
_SUFIXOS_OK = {"", "s", "es", "e", "a", "o", "as", "os", "ao", "oes", "inha", "inho", "inhas", "inhos", "zinho", "zinha"}


def _nota_palavra(tok, kw):
    """0..1: quão parecida é a palavra falada/digitada com a palavra-chave."""
    if tok == kw:
        return 1.0
    if len(tok) < 4 or len(kw) < 4:
        return 0.0
    if (tok.startswith(kw) or kw.startswith(tok)) and abs(len(tok) - len(kw)) <= 3:
        return 0.9
    if tok.startswith(kw[:-1]) and tok[len(kw) - 1:] in _SUFIXOS_OK and len(kw) >= 5:
        return 0.8
    r = difflib.SequenceMatcher(None, tok, kw).ratio()
    return r if r >= 0.84 else 0.0


def categoria_similar(t_normalizado):
    """Área mais parecida com o texto (sem acento). 'Outros' se nada se parece."""
    melhor, nota = "Outros", 0.0
    for tok in re.findall(r"[a-z0-9]+", t_normalizado):
        if len(tok) < 4:
            continue
        for cat, palavras in _PALAVRAS_SEMELHANTES.items():
            for kw in palavras.split():
                n = _nota_palavra(tok, kw)
                if n > nota:
                    melhor, nota = cat, n
    return melhor if nota >= 0.8 else "Outros"


# ------------------------------------------------------------------ descrição
_FORCAR = r"\b(?:de novo|outra vez|novamente|mesmo assim|mais um|mais uma)\b"


def _descricao(texto, remover, tipo, categoria):
    """Tira valor/data do texto por posição e limpa o que sobra."""
    s = texto
    for ini, fim in sorted(remover, reverse=True):
        s = s[:ini] + " " + s[fim:]
    s = re.sub(_FORCAR, " ", s, flags=re.I)
    s = re.sub(r"r\$|\bparcelas?\s*$", " ", s, flags=re.I)
    s = re.sub(r"^\s*[+\-]|[+\-]\s*$", " ", s)
    palavras = [p for p in s.split() if p]

    def limpa(p):
        return normalizar(p).strip(".,!?;:")

    while palavras and limpa(palavras[0]) in PALAVRAS_RUIDO:
        palavras.pop(0)
    while palavras and limpa(palavras[-1]) in PALAVRAS_RUIDO:
        palavras.pop()
    saida = []                                      # "pix de do carlos" -> "pix do carlos"
    for i, p in enumerate(palavras):
        prox = limpa(palavras[i + 1]) if i + 1 < len(palavras) else ""
        if limpa(p) in _PREPOSICOES and prox in _PREPOSICOES:
            continue
        saida.append(p)
    desc = " ".join(saida).strip(" .,;:-!?")
    if not desc:
        return categoria if categoria not in ("Outros", "") else ("Receita" if tipo == "receita" else "Despesa")
    desc = desc[:60].rstrip()
    return desc[0].upper() + desc[1:]


# ------------------------------------------------------------------ núcleo
def _analisar(original, hoje, tipo_padrao=None, data_padrao=None):
    """Um trecho com UM valor. Devolve dict (com '_explicita') ou None."""
    texto = _pre_normalizar(original)
    t = normalizar(texto)
    if len(t) != len(texto):        # caracteres raros mudaram o tamanho: usa o normalizado
        texto = t
    vals = _valores(t)
    if not vals:
        return None
    centavos, span_v = vals[0]
    if centavos <= 0 or centavos > MAX_CENTAVOS:
        return None
    m = re.match(r"\s*([+\-])\s*(?:r\$\s*)?\d", t)
    tipo = _tipo(t, m.group(1) if m else None) or tipo_padrao
    cat_prevista = None
    if not tipo:                    # "almoço 35", "uber 18,90": sem verbo, mas o assunto mostra que é gasto
        cat_prevista = categoria_para(t[:span_v[0]] + " " + t[span_v[1]:])
        if cat_prevista == "Outros":
            cat_prevista = categoria_similar(t[:span_v[0]] + " " + t[span_v[1]:])
        if cat_prevista != "Outros":
            tipo = "despesa"
    if not tipo:
        return None
    data, span_d, explicita = _extrair_data(t, hoje)
    if not explicita and data_padrao:
        data = data_padrao
    if data > hoje + timedelta(days=1) or data < hoje - timedelta(days=400):
        return None
    remover = [span_v] + ([span_d] if span_d else [])
    sem = t                         # categoria lida SEM valor/data (o "99" do valor não vira "99 Pop")
    for ini, fim in sorted(remover, reverse=True):
        sem = sem[:ini] + " " + sem[fim:]
    cat = (cat_prevista or categoria_para(sem)) if tipo == "despesa" else ""
    if tipo == "despesa" and cat == "Outros":
        cat = categoria_similar(sem)
    return {"tipo": tipo, "centavos": centavos, "descricao": _descricao(texto, remover, tipo, cat or "Outros"),
            "categoria": cat, "data": data, "forcar": bool(re.search(_FORCAR, t)), "_explicita": explicita}


def _dividir(texto):
    """Quebra a mensagem em trechos de um valor cada ('30 no lanche e 20 no uber')."""
    partes = []
    for bloco in re.split(r"[\n;]+", texto):
        bloco = bloco.strip()
        if not bloco:
            continue
        pedacos = re.split(r"(\s+e\s+|,\s+)", bloco, flags=re.I)   # mantém os separadores
        atual = pedacos[0]
        for k in range(1, len(pedacos), 2):
            sep, p = pedacos[k], pedacos[k + 1] if k + 1 < len(pedacos) else ""
            if _valores(normalizar(_pre_normalizar(p))) and _valores(normalizar(_pre_normalizar(atual))):
                partes.append(atual.strip()); atual = p
            else:
                atual += sep + p
        partes.append(atual.strip())
    return [p for p in partes if p]


def _preparar(texto):
    original = re.sub(r"[ \t]+", " ", str(texto or "")).strip()
    if len(original) > 400:
        return ""
    original = re.sub(r"(\d+)\s*(?:reais|real)\s*e\s*(\d{1,2})\s*centavos?\b", r"\1,\2 reais", original, flags=re.I)
    return _numeros_por_extenso(original)


def interpretar_varios(texto, hoje=None):
    """Lista de lançamentos de uma mensagem (vazia se não entendeu tudo)."""
    hoje = hoje or date.today()
    original = _preparar(texto)
    if not original:
        return []
    n = normalizar(original)
    if _estrangeira(n) or (re.search(r"\b\d{1,2}\s*x\b", n) and "parcel" not in n):
        return []                           # moeda estrangeira / compra parcelada: precisa de esclarecimento
    partes = _dividir(original)
    if not partes or len(partes) > MAX_ITENS:
        return []
    itens, tipo_ant, data_ant = [], None, None
    for parte in partes:
        r = _analisar(parte, hoje, tipo_ant, data_ant)
        if not r:
            return []                       # um trecho confuso: melhor perguntar do que lançar pela metade
        tipo_ant = r["tipo"]
        if r.pop("_explicita"):
            data_ant = r["data"]
        itens.append(r)
    return itens


def interpretar(texto, hoje=None):
    """Compatível com a versão anterior: o primeiro lançamento da mensagem ou None."""
    itens = interpretar_varios(texto, hoje)
    return itens[0] if itens else None


def motivo_falha(texto):
    """Por que a mensagem não virou lançamento (para o servidor explicar):
    'vazia', 'longa', 'moeda', 'parcelado', 'sem_valor', 'valor_alto', 'muitos', 'sem_tipo' ou 'outro'."""
    bruto = re.sub(r"\s+", " ", str(texto or "")).strip()
    if not bruto:
        return "vazia"
    if len(bruto) > 400:
        return "longa"
    original = _preparar(bruto)
    n = normalizar(original)
    if _estrangeira(n):
        return "moeda"
    if re.search(r"\b\d{1,2}\s*x\b", n) and "parcel" not in n:
        return "parcelado"
    vals = _valores(normalizar(_pre_normalizar(original)))
    if not vals:
        return "sem_valor"
    if any(c > MAX_CENTAVOS for c, _ in vals):
        return "valor_alto"
    if len(_dividir(original)) > MAX_ITENS:
        return "muitos"
    if _tipo(n) is None and categoria_para(n) == "Outros" and not re.match(r"\s*[+\-]", n):
        return "sem_tipo"
    return "outro"


# ------------------------------------------------------------------ comandos
_SAUDACAO = {"oi", "oii", "oiii", "oie", "ola", "olaa", "opa", "eai", "e", "ai", "bom", "boa", "dia", "tarde",
             "noite", "tudo", "bem", "td", "hey", "hello", "hi", "salve", "fala", "zenfi", "zen", "amigo", "e"}
_SAUDACAO_CHAVE = {"oi", "oii", "oiii", "oie", "ola", "olaa", "opa", "eai", "hey", "hello", "hi", "salve", "fala", "bom", "boa"}
_AGRADECE = {"obrigado", "obrigada", "brigado", "brigada", "valeu", "vlw", "thanks", "grato", "grata", "muito", "demais", "tmj"}
_CIENTE = {"ok", "okay", "blz", "beleza", "certo", "entendi", "show", "top", "perfeito", "otimo", "legal", "massa",
           "combinado", "fechado", "sim", "isso", "👍", "🙏", "😉", "👏", "❤️", "😊", "😀"}


def comando(texto):
    """Mensagens que NÃO são lançamentos. Devolve (nome, extras) ou None.
    nomes: desfazer, ajuda, extrato, resumo, saldo, saudacao, agradecimento, ciente.
    extras: {'mes': 'passado'} para 'resumo do mês passado'."""
    bruto = str(texto or "").strip()
    if bruto in ("?", "¿", "??"):
        return "ajuda", {}
    t = normalizar(re.sub(r"[!.,…?¿;:]+", " ", bruto))
    if not t or len(t) > 60 or re.search(r"\d", t):
        return None
    pal = t.split()
    extras = {"mes": "passado"} if re.search(r"\bmes (?:passado|anterior)\b", t) else {}
    n = len(pal)
    if n <= 5 and re.fullmatch(r"(?:desfaz\w*|desfeito|errei|apaga\w*|cancela\w*|exclu\w*|remov\w*)"
                               r"(?: (?:o|a|isso|esse|essa|ultimo|ultima|lancamento|registro|por favor|pf))*", t):
        return "desfazer", {}
    if n <= 5 and re.search(r"\b(?:ajuda|help|menu|comandos?|opcoes|instrucoes)\b|\bcomo (?:funciona|usar|uso)\b|\bo que (?:voce|vc) (?:faz|sabe)", t):
        return "ajuda", {}
    if n <= 5 and re.search(r"\b(?:extrato|ultimos|ultimas|historico|movimentacoes|lancamentos)\b", t):
        return "extrato", {}
    if n <= 7 and re.search(r"\b(?:resumo|relatorio|balanco)\b|\bquanto gastei\b|\bonde gastei\b|\b(?:gastos|despesas) (?:do|desse|deste|no) mes\b", t):
        return "resumo", extras
    if n <= 6 and re.search(r"\bsaldo\b|\bquanto (?:tenho|sobrou|resta|sobra|me resta)\b|\bcomo estou\b", t):
        return "saldo", extras
    if n <= 5 and set(pal) <= _SAUDACAO and set(pal) & _SAUDACAO_CHAVE:
        return "saudacao", {}
    if n <= 5 and set(pal) <= (_AGRADECE | _CIENTE | {"e", "de", "nada", "a", "o"}):
        return ("agradecimento", {}) if set(pal) & _AGRADECE else ("ciente", {})
    return None
