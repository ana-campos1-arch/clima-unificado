# ==========================================================
# CLIMA UNIFICADO - INMET + OPEN-METEO
# Versão robusta para deploy no Render + Google Sheets
#
# COMO CONFIGURAR (leia antes de rodar):
#
# ── Google Sheets ────────────────────────────────────────
#  1. Acesse https://console.cloud.google.com
#  2. Crie um projeto > ative "Google Sheets API" e
#     "Google Drive API"
#  3. Crie uma "Service Account" e baixe o JSON de credenciais
#  4. No Render, use "Secret Files" para subir esse JSON com o
#     nome "credenciais.json" (fica em /etc/secrets/credenciais.json)
#  5. Crie uma planilha no Google Sheets e compartilhe com
#     o e-mail da Service Account (papel: Editor)
#  6. Configure GSHEETS_NOME e GSHEETS_LINK como variáveis de
#     ambiente no Render (ou deixe os valores padrão abaixo)
#
# O QUE MUDOU NESTA VERSÃO
# ─────────────────────────────────────────────────────────
#  • MUDANÇA PRINCIPAL: o INMET agora é coletado pela API de TEMPO REAL
#    (apitempo.inmet.gov.br), que devolve dado hora a hora direto da
#    estação — assim como o Open-Meteo. O ZIP histórico anual (que passa
#    por controle de qualidade e fica 1-3 semanas atrasado) só entra
#    como RESERVA automática, caso a API de tempo real esteja fora do
#    ar ou pare de responder para todas as estações.
#  • O caminho de reserva (ZIP) continua comparando por HASH do
#    conteúdo baixado (não cabeçalhos HTTP, que não são confiáveis).
#  • Se o ZIP do ano corrente ainda não existir no INMET (comum no
#    início de janeiro), o caminho de reserva cai para o ano anterior.
#  • Chamadas de rede (Open-Meteo, INMET, Google Sheets) têm retries
#    com backoff exponencial.
#  • Logging estruturado (nível, hora) no lugar de print().
#  • Google Sheets só reescreve uma aba se os dados dela realmente
#    mudaram (evita bater no limite de requisições da API).
#  • /status mostra a data/hora mais recente de cada fonte de dados,
#    reconhecendo tanto os nomes de coluna do ZIP quanto da API.
# ==========================================================

import io
import logging
import os
import threading
import time
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from flask import Flask, request as flask_request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================================
# LOGGING
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
)
log = logging.getLogger("clima")

# ==========================================================
# ⚙️  CONFIGURAÇÕES PRINCIPAIS
# ==========================================================

LATITUDE  = -30.0397
LONGITUDE = -52.8930

ESTACOES = {
    "B822":  "Cachoeira do Sul",
    "A803":  "Santa Maria Automática",
    "83936": "Santa Maria Convencional",
}

ANO_ATUAL = date.today().year
URL_INMET_BASE = "https://portal.inmet.gov.br/uploads/dadoshistoricos/{ano}.zip"

# De quantas em quantas horas o ciclo de atualização roda. Vale tanto para
# Open-Meteo quanto para o INMET, já que agora o INMET usa a API de tempo
# real (hora a hora), no mesmo ritmo do Open-Meteo. O ZIP histórico anual
# só entra como reserva automática se a API do INMET falhar (veja a seção
# "INMET" mais abaixo).
INTERVALO_ATUALIZACAO_HORAS = 1

# ── Google Sheets ────────────────────────────────────────
GSHEETS_ATIVO       = True
GSHEETS_CREDENCIAIS = os.environ.get("GSHEETS_CREDENCIAIS_PATH", "/etc/secrets/credenciais.json")
GSHEETS_NOME        = os.environ.get("GSHEETS_NOME", "Clima Unificado")
GSHEETS_LINK        = os.environ.get(
    "GSHEETS_LINK",
    "https://docs.google.com/spreadsheets/d/1yDFMkt0-Buuijc1LwVc6Sj8Zixk74cc0izi0azvT5sA/edit?gid=192990081#gid=192990081",
)

# ── Estação Meteorológica (planilha externa, fonte adicional) ──────
# Essa é uma planilha DIFERENTE da planilha de destino acima — é onde
# a estação física já registra os dados. O app só LÊ dela e copia os
# dados para dentro da tabela unificada / da planilha de destino.
ESTACAO_METEO_SHEET_ID = os.environ.get(
    "ESTACAO_METEO_SHEET_ID",
    "1t2ZztZ7zBMZD148G4Ib6USTTkVVe4hWnS-CGgEd7CQM",
)
# Nome da aba a ler dentro dessa planilha externa. Deixe em branco para
# usar a primeira aba automaticamente.
ESTACAO_METEO_ABA = os.environ.get("ESTACAO_METEO_ABA", "")

# ==========================================================
# FONTE → CLASSE CSS
# ==========================================================

FONTE_CSS = {
    "Open-Meteo – Hoje (horário)":       "src-om-hora",
    "Open-Meteo – Previsão (diária)":    "src-om-prev",
    "INMET - Cachoeira do Sul":          "src-cachoeira",
    "INMET - Santa Maria Automática":    "src-sm-auto",
    "INMET - Santa Maria Convencional":  "src-sm-conv",
    "Estação Meteorológica":             "src-estacao-meteo",
}

# ==========================================================
# SESSÃO HTTP COM RETRY/BACKOFF
# ==========================================================

def criar_sessao():
    sessao = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,          # 2s, 4s, 8s, 16s entre tentativas
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adaptador = HTTPAdapter(max_retries=retry)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    return sessao

sessao_http = criar_sessao()

# ==========================================================
# OPEN-METEO: HOJE HORA A HORA
# ==========================================================

def coletar_om_horario():
    log.info("Buscando Open-Meteo – hoje hora a hora...")
    hoje = date.today().isoformat()
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LATITUDE}&longitude={LONGITUDE}"
        f"&hourly=temperature_2m,relative_humidity_2m,"
        f"wind_speed_10m,precipitation,weather_code"
        f"&start_date={hoje}&end_date={hoje}"
        f"&timezone=America/Sao_Paulo"
    )
    try:
        dados = sessao_http.get(url, timeout=30).json()
        h = dados["hourly"]
        linhas = []
        for i, hora in enumerate(h["time"]):
            linhas.append({
                "Estacao":          "Open-Meteo – Hoje (horário)",
                "Data":             hora[:10],
                "Hora":             hora[11:] + ":00",
                "Temperatura (°C)": h["temperature_2m"][i],
                "Umidade (%RH)":    h["relative_humidity_2m"][i],
                "Vento (km/h)":     h["wind_speed_10m"][i],
                "Precip. (mm)":     h["precipitation"][i],
                "Cód. Clima":       h["weather_code"][i],
            })
        log.info(f"OK: {len(linhas)} horas do dia de hoje (Open-Meteo)")
        df = pd.DataFrame(linhas)
        _marcar_diagnostico("Open-Meteo – Hoje (horário)", True, len(df))
        return df
    except Exception as e:
        log.error(f"Erro Open-Meteo horário: {e}")
        _marcar_diagnostico("Open-Meteo – Hoje (horário)", False, 0, str(e))
        return pd.DataFrame()

# ==========================================================
# OPEN-METEO: PREVISÃO DIÁRIA (próximos 16 dias)
# ==========================================================

def coletar_om_diario():
    log.info("Buscando Open-Meteo – previsão diária...")
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LATITUDE}&longitude={LONGITUDE}"
        f"&daily=temperature_2m_max,temperature_2m_min,"
        f"precipitation_sum,wind_speed_10m_max,"
        f"weather_code,sunrise,sunset"
        f"&forecast_days=16"
        f"&timezone=America/Sao_Paulo"
    )
    try:
        dados = sessao_http.get(url, timeout=30).json()
        d = dados["daily"]
        linhas = []
        for i, dia in enumerate(d["time"]):
            linhas.append({
                "Estacao":           "Open-Meteo – Previsão (diária)",
                "Data":              dia,
                "Hora":              "—",
                "Temp. Máx (°C)":    d["temperature_2m_max"][i],
                "Temp. Mín (°C)":    d["temperature_2m_min"][i],
                "Precip. (mm)":      d["precipitation_sum"][i],
                "Vento Máx (km/h)":  d["wind_speed_10m_max"][i],
                "Cód. Clima":        d["weather_code"][i],
                "Nascer do Sol":     d["sunrise"][i],
                "Pôr do Sol":        d["sunset"][i],
            })
        log.info(f"OK: {len(linhas)} dias de previsão (Open-Meteo)")
        df = pd.DataFrame(linhas)
        _marcar_diagnostico("Open-Meteo – Previsão (diária)", True, len(df))
        return df
    except Exception as e:
        log.error(f"Erro Open-Meteo diário: {e}")
        _marcar_diagnostico("Open-Meteo – Previsão (diária)", False, 0, str(e))
        return pd.DataFrame()

# ==========================================================
# GSPREAD — CLIENTE COMPARTILHADO
# ==========================================================

_gspread_cliente_cache = None

def _obter_cliente_gspread():
    """
    Cria (uma única vez) e reutiliza o cliente autenticado do gspread,
    tanto para ler a planilha da Estação Meteorológica quanto para
    escrever na planilha de destino.
    """
    global _gspread_cliente_cache
    if _gspread_cliente_cache is not None:
        return _gspread_cliente_cache

    import gspread
    from google.oauth2.service_account import Credentials

    if not os.path.exists(GSHEETS_CREDENCIAIS):
        raise RuntimeError(f"Credenciais não encontradas em {GSHEETS_CREDENCIAIS}")

    escopos = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GSHEETS_CREDENCIAIS, scopes=escopos)
    _gspread_cliente_cache = gspread.authorize(creds)
    return _gspread_cliente_cache

# ==========================================================
# ESTAÇÃO METEOROLÓGICA (planilha externa, fonte adicional)
# ==========================================================

def coletar_estacao_meteorologica():
    """
    Lê os dados já lançados na planilha da estação meteorológica física
    e devolve como DataFrame, com a coluna "Estacao" preenchida, para
    entrar junto na tabela unificada.

    IMPORTANTE: essa planilha externa precisa estar compartilhada (papel:
    Leitor ou Editor) com o e-mail da mesma Service Account usada nas
    outras credenciais do Google Sheets — senão a leitura falha com erro
    de permissão.
    """
    if not ESTACAO_METEO_SHEET_ID:
        return pd.DataFrame()

    log.info("Buscando dados da Estação Meteorológica (planilha externa)...")
    try:
        gc = _obter_cliente_gspread()
        sh = gc.open_by_key(ESTACAO_METEO_SHEET_ID)
        ws = sh.worksheet(ESTACAO_METEO_ABA) if ESTACAO_METEO_ABA else sh.sheet1
        registros = ws.get_all_records()
        if not registros:
            log.warning("Planilha da Estação Meteorológica está vazia.")
            return pd.DataFrame()
        df = pd.DataFrame(registros)
        df.insert(0, "Estacao", "Estação Meteorológica")
        log.info(f"OK: {len(df)} registros da Estação Meteorológica")
        _marcar_diagnostico("Estação Meteorológica", True, len(df))
        return df
    except Exception as e:
        log.error(f"Erro ao ler a planilha da Estação Meteorológica: {e}")
        _marcar_diagnostico("Estação Meteorológica", False, 0, str(e))
        return pd.DataFrame()

# ==========================================================
# INMET — API de tempo real (fonte principal) + ZIP anual (reserva)
# ==========================================================
#
# O INMET tem duas fontes bem diferentes:
#   1) API de tempo real (apitempo.inmet.gov.br) — dado hora a hora,
#      direto da estação, sem passar pelo controle de qualidade demorado.
#      É esta que faz o INMET atualizar "como o Open-Meteo".
#   2) ZIP histórico anual (dadoshistoricos) — passa por controle de
#      qualidade, por isso fica 1-3 semanas atrasado. Usado aqui só como
#      RESERVA, caso a API de tempo real esteja fora do ar ou tenha
#      mudado de contrato (isso já aconteceu antes com bibliotecas que
#      dependem dela, então a checagem por hash de conteúdo continua
#      valendo para esse caminho de reserva).

URL_INMET_API_BASE = "https://apitempo.inmet.gov.br/estacao/{inicio}/{fim}/{codigo}"

def coletar_inmet_api():
    """
    Busca o dado hora a hora mais recente de cada estação via API de
    tempo real do INMET. Pede os últimos 2 dias (não só "hoje") como
    margem de segurança contra atraso de transmissão da estação/fuso.
    Retorna um DataFrame vazio se a API não responder para NENHUMA
    estação (sinal de que ela pode estar fora do ar/mudou de contrato).
    """
    hoje    = date.today()
    inicio  = (hoje - timedelta(days=2)).isoformat()
    fim     = hoje.isoformat()

    blocos = []
    for codigo, cidade in ESTACOES.items():
        url = URL_INMET_API_BASE.format(inicio=inicio, fim=fim, codigo=codigo)
        fonte_diag = f"INMET (API) - {cidade}"
        try:
            resp = sessao_http.get(url, timeout=30)
            resp.raise_for_status()
            registros = resp.json()
            if not registros or not isinstance(registros, list):
                log.warning(f"API de tempo real do INMET sem dados para {cidade} ({codigo}) no período pedido.")
                _marcar_diagnostico(fonte_diag, False, 0, "resposta vazia/sem lista")
                continue
            df = pd.DataFrame(registros)
            if df.empty:
                _marcar_diagnostico(fonte_diag, False, 0, "DataFrame vazio")
                continue
            df.insert(0, "Estacao", f"INMET - {cidade}")
            blocos.append(df)
            _marcar_diagnostico(fonte_diag, True, len(df))
            log.info(f"OK: INMET (API tempo real) - {cidade}: {len(df)} registros")
        except Exception as e:
            log.warning(f"Erro ao consultar API de tempo real do INMET para {cidade} ({codigo}): {e}")
            _marcar_diagnostico(fonte_diag, False, 0, str(e))

    if not blocos:
        return pd.DataFrame()
    return pd.concat(blocos, ignore_index=True)

# ── Reserva: ZIP histórico anual, com checagem por hash de conteúdo ──

_inmet_zip_hash_cache = {}
_inmet_zip_url_ativa  = None

def _resolver_url_inmet_zip(sessao):
    global _inmet_zip_url_ativa
    candidatos = [ANO_ATUAL, ANO_ATUAL - 1]
    for ano in candidatos:
        url = URL_INMET_BASE.format(ano=ano)
        try:
            sessao.head(url, timeout=30, allow_redirects=True).raise_for_status()
            if _inmet_zip_url_ativa != url:
                log.info(f"Usando ZIP do INMET (reserva): {ano}")
            _inmet_zip_url_ativa = url
            return url
        except Exception:
            log.warning(f"ZIP do INMET para {ano} indisponível, tentando outro ano...")
    raise RuntimeError("Nenhum ZIP do INMET disponível (ano atual nem anterior).")

def coletar_inmet_zip_reserva(forcar=False):
    """
    Caminho de reserva: baixa o ZIP histórico anual e só reprocessa se o
    CONTEÚDO mudou (hash SHA-256) desde a última vez. Só é chamado quando
    a API de tempo real falha para todas as estações.
    """
    import hashlib

    try:
        url = _resolver_url_inmet_zip(sessao_http)
    except Exception as e:
        log.error(f"Erro ao resolver URL do ZIP do INMET: {e}")
        return pd.DataFrame()

    log.info("Baixando ZIP do INMET (reserva) para checagem...")
    try:
        resposta = sessao_http.get(url, timeout=180)
        resposta.raise_for_status()
        conteudo = resposta.content
    except Exception as e:
        log.error(f"Erro ao baixar ZIP do INMET (reserva): {e}")
        return pd.DataFrame()

    hash_atual    = hashlib.sha256(conteudo).hexdigest()
    hash_anterior = _inmet_zip_hash_cache.get(url)

    if not forcar and hash_anterior == hash_atual:
        log.info("INMET (reserva): conteúdo do ZIP idêntico ao da última coleta.")
        return pd.DataFrame()

    try:
        zip_file = zipfile.ZipFile(io.BytesIO(conteudo))
        arquivos = zip_file.namelist()
        dados = []
        for codigo, cidade in ESTACOES.items():
            for arq in arquivos:
                if codigo in arq:
                    try:
                        df = pd.read_csv(
                            zip_file.open(arq),
                            sep=";", encoding="latin1",
                            skiprows=8, low_memory=False,
                        )
                        df.insert(0, "Estacao", f"INMET - {cidade}")
                        dados.append(df)
                        log.info(f"OK: INMET (reserva/ZIP) - {cidade}")
                    except Exception as e:
                        log.error(f"Erro ao processar estação {cidade} (reserva/ZIP): {e}")

        if not dados:
            return pd.DataFrame()

        _inmet_zip_hash_cache[url] = hash_atual
        return pd.concat(dados, ignore_index=True)
    except Exception as e:
        log.error(f"Erro ao processar ZIP do INMET (reserva): {e}")
        return pd.DataFrame()

def coletar_inmet(forcar=False):
    """
    Ponto de entrada único usado por montar_tabela(). Tenta a API de
    tempo real primeiro (dado hora a hora). Se ela não devolver nada
    para nenhuma estação, cai automaticamente para o ZIP histórico como
    reserva. Retorna: (DataFrame, houve_atualizacao: bool)
    """
    df_api = coletar_inmet_api()
    if not df_api.empty:
        _marcar_diagnostico("INMET (fonte usada)", True, len(df_api), "API de tempo real")
        return df_api, True

    log.warning("API de tempo real do INMET não retornou dados para nenhuma estação; "
                "usando ZIP histórico como reserva.")
    df_zip = coletar_inmet_zip_reserva(forcar=forcar)
    if not df_zip.empty:
        _marcar_diagnostico("INMET (fonte usada)", True, len(df_zip), "ZIP histórico (reserva)")
    else:
        _marcar_diagnostico("INMET (fonte usada)", False, 0, "API e ZIP falharam/sem novidade")
    return df_zip, (not df_zip.empty)

def encontrar_coluna(colunas, *chaves):
    for col in colunas:
        nome = str(col).upper()
        if all(chave.upper() in nome for chave in chaves):
            return col
    return None

# ==========================================================
# ESTADO COMPARTILHADO
# ==========================================================

tabela             = pd.DataFrame()
tabela_lock        = threading.Lock()
ultima_atualizacao = None
_ultimo_bloco_inmet = pd.DataFrame()

# ==========================================================
# DIAGNÓSTICO — registra o resultado de cada coleta, fonte a fonte
# ==========================================================
# Serve pra gerar a aba "Diagnóstico" no Sheets, que muda a cada ciclo
# (tem timestamp), então é uma forma visual de confirmar que o app está
# rodando de verdade, sem precisar abrir logs do Render.

_diagnostico_lock = threading.Lock()
_diagnostico = {}  # fonte -> {"ok": bool, "registros": int, "detalhe": str, "hora": datetime}

def _marcar_diagnostico(fonte, ok, registros=0, detalhe=""):
    with _diagnostico_lock:
        _diagnostico[fonte] = {
            "ok": ok,
            "registros": registros,
            "detalhe": detalhe,
            "hora": datetime.now(),
        }

def montar_aba_diagnostico():
    """Monta um DataFrame simples pra aba 'Diagnóstico' do Sheets."""
    agora = datetime.now()
    linhas = [{
        "Fonte": "── ÚLTIMA EXECUÇÃO DO CICLO ──",
        "Status": "🟢 RODANDO",
        "Registros": "",
        "Detalhe": f"Ciclo iniciado às {agora.strftime('%d/%m/%Y %H:%M:%S')}",
        "Checado em": agora.strftime("%d/%m/%Y %H:%M:%S"),
    }]
    with _diagnostico_lock:
        for fonte, info in sorted(_diagnostico.items()):
            linhas.append({
                "Fonte": fonte,
                "Status": "✅ OK" if info["ok"] else "❌ FALHOU",
                "Registros": info["registros"],
                "Detalhe": info["detalhe"],
                "Checado em": info["hora"].strftime("%d/%m/%Y %H:%M:%S"),
            })
    return pd.DataFrame(linhas)

# ==========================================================
# MONTAR TABELA UNIFICADA
# ==========================================================

def montar_tabela(forcar_inmet=False):
    """
    forcar_inmet: repassado para o caminho de reserva (ZIP anual), caso a
    API de tempo real esteja indisponível — ignora o cache de hash e força
    reprocessar o ZIP. Não tem efeito quando a API de tempo real responde
    normalmente (que é o caso mais comum).
    """
    global _ultimo_bloco_inmet

    df_hora    = coletar_om_horario()
    df_prev    = coletar_om_diario()
    df_estacao = coletar_estacao_meteorologica()

    # A API de tempo real do INMET traz dado hora a hora, então é checada
    # a cada ciclo — igual o Open-Meteo. Se ela falhar, coletar_inmet()
    # cai sozinha para o ZIP anual como reserva (esse sim mais raro de
    # mudar, mas ainda assim comparado por hash pra não reprocessar à toa).
    df_inmet_novo, inmet_mudou = coletar_inmet(forcar=forcar_inmet)
    if inmet_mudou and not df_inmet_novo.empty:
        _ultimo_bloco_inmet = df_inmet_novo
    else:
        log.info("INMET: usando o último bloco de dados válido dessa fonte (nenhuma novidade neste ciclo).")

    df_inmet = _ultimo_bloco_inmet

    partes = [df for df in [df_hora, df_prev, df_estacao, df_inmet] if not df.empty]
    if not partes:
        return pd.DataFrame()

    tabela_montada = pd.concat(partes, ignore_index=True)
    cols = ["Estacao"] + [c for c in tabela_montada.columns if c != "Estacao"]
    return tabela_montada[cols]

# ==========================================================
# GOOGLE SHEETS
# ==========================================================

_hash_abas_enviadas = {}

def _hash_dataframe(df):
    return pd.util.hash_pandas_object(df.fillna("—").astype(str)).sum()

def _escrever_aba_com_retry(sh, nome_aba, dados, tentativas=3, pular_dedup=False):
    import gspread

    novo_hash = _hash_dataframe(dados)
    if not pular_dedup and _hash_abas_enviadas.get(nome_aba) == novo_hash:
        log.info(f"Aba '{nome_aba}' sem mudanças; pulando escrita no Sheets.")
        return

    dados_str = dados.fillna("—").astype(str)
    linhas    = [dados_str.columns.tolist()] + dados_str.values.tolist()

    for tentativa in range(1, tentativas + 1):
        try:
            try:
                ws = sh.worksheet(nome_aba)
                ws.clear()
            except gspread.exceptions.WorksheetNotFound:
                ws = sh.add_worksheet(title=nome_aba, rows=len(linhas) + 10, cols=len(dados_str.columns))
            ws.update(linhas)
            _hash_abas_enviadas[nome_aba] = novo_hash
            return
        except gspread.exceptions.APIError as e:
            espera = 5 * tentativa
            log.warning(f"Rate limit/erro do Sheets na aba '{nome_aba}' (tentativa {tentativa}/{tentativas}); "
                        f"aguardando {espera}s... ({e})")
            time.sleep(espera)
        except Exception as e:
            log.error(f"Erro inesperado ao escrever aba '{nome_aba}': {e}")
            return
    log.error(f"Falha ao escrever aba '{nome_aba}' após {tentativas} tentativas.")

def exportar_para_sheets(df):
    try:
        import gspread  # noqa: F401 (garante que a lib está instalada)
    except ImportError:
        log.warning("gspread/google-auth não instalados. Verifique o requirements.txt.")
        return

    if not os.path.exists(GSHEETS_CREDENCIAIS):
        log.warning(f"Credenciais não encontradas em {GSHEETS_CREDENCIAIS}. Configure o Secret File no Render.")
        return

    try:
        gc = _obter_cliente_gspread()
        sh = gc.open(GSHEETS_NOME)

        # A aba Diagnóstico é escrita SEMPRE, mesmo se a tabela principal
        # estiver vazia — é ela que prova visualmente que o ciclo rodou,
        # porque tem um timestamp que muda a cada execução.
        _escrever_aba_com_retry(sh, "Diagnóstico", montar_aba_diagnostico(), pular_dedup=True)

        if df.empty:
            log.warning("Tabela principal vazia; só a aba Diagnóstico foi atualizada neste ciclo.")
            return

        _escrever_aba_com_retry(sh, "Todas", df)

        for fonte in df["Estacao"].unique():
            df_f = df[df["Estacao"] == fonte]
            cols = [c for c in df.columns if df_f[c].notna().any()]
            _escrever_aba_com_retry(sh, fonte[:100], df_f[cols])

        log.info(f"Google Sheets sincronizado: '{GSHEETS_NOME}'")
    except Exception as e:
        log.error(f"Erro ao exportar para Google Sheets: {e}")

# ==========================================================
# ATUALIZAÇÃO DE DADOS
# ==========================================================

def atualizar_dados(forcar_inmet=False):
    global tabela, ultima_atualizacao
    log.info("Iniciando ciclo de atualização dos dados...")
    nova_tabela = montar_tabela(forcar_inmet=forcar_inmet)
    with tabela_lock:
        if not nova_tabela.empty:
            tabela = nova_tabela
            ultima_atualizacao = datetime.now()
            log.info(f"Atualização concluída: {len(tabela)} registros.")
        else:
            log.warning("A atualização não trouxe dados novos; mantendo a tabela anterior.")

    if GSHEETS_ATIVO:
        with tabela_lock:
            df_copia = tabela.copy()
        exportar_para_sheets(df_copia)

# ==========================================================
# AGENDADOR
# ==========================================================

def proxima_execucao():
    agora = datetime.now()
    base  = agora.replace(minute=0, second=0, microsecond=0)
    return base + timedelta(hours=INTERVALO_ATUALIZACAO_HORAS)

def agendador():
    while True:
        alvo     = proxima_execucao()
        segundos = (alvo - datetime.now()).total_seconds()
        log.info(f"Próxima atualização automática agendada para {alvo.strftime('%d/%m/%Y às %H:%M')}")
        time.sleep(max(segundos, 1))
        try:
            atualizar_dados()
        except Exception as e:
            log.error(f"Erro inesperado no ciclo de atualização: {e}")

# ==========================================================
# FLASK
# ==========================================================

app = Flask(__name__)

@app.route("/")
def inicio():
    with tabela_lock:
        df_atual = tabela.copy()

    if df_atual.empty:
        return "<h1>Buscando os dados pela primeira vez... atualize a página em alguns segundos.</h1>"

    fontes      = list(df_atual["Estacao"].unique())
    fonte_ativa = flask_request.args.get("fonte", "todas")

    df_exibir = df_atual if fonte_ativa == "todas" else \
                df_atual[df_atual["Estacao"] == fonte_ativa]

    if fonte_ativa == "todas":
        colunas_mostrar = list(df_atual.columns)
    else:
        colunas_mostrar = [c for c in df_atual.columns if df_exibir[c].notna().any()]
        if "Estacao" not in colunas_mostrar:
            colunas_mostrar = ["Estacao"] + colunas_mostrar

    cabecalho_html = "".join(f"<th>{col}</th>" for col in colunas_mostrar)
    linhas_html    = gerar_linhas(df_exibir[colunas_mostrar].fillna("—").head(500))

    botoes = f'<a href="/" class="btn btn-todas {"btn-ativo" if fonte_ativa=="todas" else ""}">🌐 Todas</a>\n'
    icones = {
        "Open-Meteo – Hoje (horário)":      "🕐",
        "Open-Meteo – Previsão (diária)":   "📅",
        "Estação Meteorológica":            "🌡️",
        "INMET - Cachoeira do Sul":         "📡",
        "INMET - Santa Maria Automática":   "📡",
        "INMET - Santa Maria Convencional": "📡",
    }
    for fonte in fontes:
        css   = FONTE_CSS.get(fonte, "")
        ativo = "btn-ativo" if fonte == fonte_ativa else ""
        ico   = icones.get(fonte, "")
        url   = f"/?fonte={requests.utils.quote(fonte)}"
        botoes += f'<a href="{url}" class="btn {css} {ativo}">{ico} {fonte}</a>\n'

    total   = len(df_atual)
    exibido = min(500, len(df_exibir))
    rodape_atualizacao = (
        ultima_atualizacao.strftime("%d/%m/%Y às %H:%M:%S")
        if ultima_atualizacao else "—"
    )

    banner_sheets = ""
    if GSHEETS_ATIVO:
        banner_sheets = f"""
        <div class="banner-gs">
            📊 Dados sincronizados com o Google Sheets:&nbsp;
            <a href="{GSHEETS_LINK}" target="_blank"><strong>Abrir planilha →</strong></a>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Clima Unificado</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f0f2f5; }}
        h1   {{ color: #1565C0; margin-bottom: 4px; }}
        p    {{ margin: 4px 0 14px; color: #555; font-size: 13px; }}

        .banner-gs {{
            background: #E8F5E9; border: 1px solid #A5D6A7;
            border-radius: 8px; padding: 10px 16px; margin-bottom: 14px;
            font-size: 13px; color: #1B5E20;
        }}
        .banner-gs a {{ color: #1B5E20; }}

        .filtros {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }}
        .btn {{
            padding: 7px 16px; border-radius: 20px; font-size: 13px;
            font-weight: bold; text-decoration: none; border: 2px solid transparent;
            cursor: pointer; transition: opacity .15s, border-color .15s;
        }}
        .btn:hover  {{ opacity: .8; }}
        .btn-ativo  {{ border-color: #222 !important; }}
        .btn-todas  {{ background: #e0e0e0; color: #333; }}

        .src-om-hora        {{ background: #B3E5FC; color: #01579B; }}
        .src-om-prev        {{ background: #C5CAE9; color: #1A237E; }}
        .src-estacao-meteo  {{ background: #FFE0B2; color: #E65100; }}
        .src-cachoeira      {{ background: #C8E6C9; color: #1B5E20; }}
        .src-sm-auto        {{ background: #FFF9C4; color: #E65100; }}
        .src-sm-conv        {{ background: #F8BBD0; color: #880E4F; }}

        .wrapper {{ overflow-x: auto; }}
        table    {{ border-collapse: collapse; width: 100%; font-size: 12px; background: white; min-width: 900px; }}
        thead th {{
            background: #1565C0; color: white; padding: 8px 6px;
            position: sticky; top: 0; white-space: nowrap;
        }}
        td {{ border: 1px solid #ddd; padding: 5px 6px; text-align: center; white-space: nowrap; }}

        tr.src-om-hora       td {{ background: #E1F5FE; }}
        tr.src-om-prev       td {{ background: #E8EAF6; }}
        tr.src-estacao-meteo td {{ background: #FFF3E0; }}
        tr.src-cachoeira     td {{ background: #E8F5E9; }}
        tr.src-sm-auto       td {{ background: #FFFDE7; }}
        tr.src-sm-conv       td {{ background: #FCE4EC; }}
    </style>
</head>
<body>
    <h1>🌦️ Clima Unificado – INMET + Open-Meteo</h1>
    <p>Total de registros: <strong>{total}</strong> &nbsp;|&nbsp;
       Exibindo: <strong>{exibido}</strong> &nbsp;|&nbsp;
       Fonte: <strong>{fonte_ativa}</strong> &nbsp;|&nbsp;
       Última atualização: <strong>{rodape_atualizacao}</strong></p>

    {banner_sheets}

    <div class="filtros">
        {botoes}
    </div>

    <div class="wrapper">
        <table>
            <thead><tr>{cabecalho_html}</tr></thead>
            <tbody>{linhas_html}</tbody>
        </table>
    </div>
</body>
</html>"""

def _data_mais_recente_por_fonte(df):
    """
    Para cada fonte na tabela, tenta achar a data/hora mais recente presente
    nos dados. Reconhece tanto os nomes de coluna do ZIP histórico
    ('Data', 'Hora') quanto os da API de tempo real do INMET
    ('DT_MEDICAO', 'HR_MEDICAO'). Usado só para diagnóstico no /status —
    não afeta a lógica de coleta/atualização.
    """
    resultado = {}
    if df.empty:
        return resultado

    for fonte in df["Estacao"].unique():
        df_f = df[df["Estacao"] == fonte]
        col_data = encontrar_coluna(df_f.columns, "DATA") or encontrar_coluna(df_f.columns, "DT", "MEDICAO")
        if col_data is None:
            resultado[fonte] = "coluna de data não encontrada"
            continue
        try:
            valores_data = df_f[col_data].astype(str)
            col_hora = encontrar_coluna(df_f.columns, "HORA") or encontrar_coluna(df_f.columns, "HR", "MEDICAO")
            if col_hora is not None:
                valores = (valores_data + " " + df_f[col_hora].astype(str)).tolist()
            else:
                valores = valores_data.tolist()
            resultado[fonte] = max(valores) if valores else "sem dados"
        except Exception as e:
            resultado[fonte] = f"erro ao calcular: {e}"
    return resultado

@app.route("/status")
def status():
    """
    Endpoint para checar se o servidor está vivo (útil para keep-alive) e,
    mais importante: mostra a data/hora mais recente presente em cada
    fonte de dados. Use isso pra confirmar rapidamente se o INMET está
    atrasado na fonte (limitação deles) ou se o app parou de atualizar
    (bug/deploy), sem precisar abrir o Sheets ou os Logs do Render.
    """
    with tabela_lock:
        df_atual = tabela.copy()
    return {
        "ok": True,
        "ultima_atualizacao_do_app": str(ultima_atualizacao),
        "dado_mais_recente_por_fonte": _data_mais_recente_por_fonte(df_atual),
        "total_registros": len(df_atual),
    }

@app.route("/atualizar")
def forcar_atualizacao():
    """
    Dispara uma atualização manual (útil para testar sem esperar o agendador).
    O INMET agora usa a API de tempo real a cada ciclo, então normalmente
    não precisa de nenhum parâmetro especial. Use /atualizar?inmet=1 apenas
    se quiser forçar o caminho de RESERVA (ZIP anual) a ignorar o cache de
    hash e reprocessar mesmo sem mudança — útil só se a API de tempo real
    estiver fora do ar e você quiser testar o fallback.
    """
    forcar_inmet = flask_request.args.get("inmet") == "1"
    threading.Thread(target=atualizar_dados, kwargs={"forcar_inmet": forcar_inmet}, daemon=True).start()
    mensagem = "Atualização disparada em segundo plano."
    if forcar_inmet:
        mensagem += " Cache de reserva (ZIP) forçado a reprocessar, se for usado."
    return {"ok": True, "mensagem": mensagem}

def gerar_linhas(df):
    html = ""
    for _, row in df.iterrows():
        estacao = str(row.get("Estacao", ""))
        css     = FONTE_CSS.get(estacao, "")
        celulas = "".join(f"<td>{v}</td>" for v in row)
        html   += f'<tr class="row {css}">{celulas}</tr>\n'
    return html

# ==========================================================
# EXECUÇÃO PRINCIPAL
# ==========================================================

if __name__ == "__main__":
    log.info("Buscando dados iniciais...")
    atualizar_dados()

    threading.Thread(target=agendador, daemon=True).start()

    porta = int(os.environ.get("PORT", 5000))
    log.info(f"Servidor iniciado na porta {porta}")
    if GSHEETS_ATIVO:
        log.info(f"PLANILHA GOOGLE SHEETS: {GSHEETS_LINK}")
    app.run(host="0.0.0.0", port=porta, threaded=True)
