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
#  • NOVO: aba "📈 Gráficos" na interface principal (ao lado das abas
#    do Open-Meteo e das demais fontes). Mostra a temperatura de hoje
#    hora a hora e as máximas/mínimas da semana sem precisar abrir a
#    tabela. Os gráficos são desenhados no navegador (Chart.js), então
#    o servidor não monta tabela nem chama o Google Sheets nessa aba.
#  • Aba "Gráficos" na planilha de destino (gráficos nativos do Google
#    Sheets) continua sendo atualizada normalmente.
#  • O INMET é coletado pela API de TEMPO REAL (apitempo.inmet.gov.br).
#    O ZIP histórico anual só entra como RESERVA automática.
#  • Reserva (ZIP) compara por HASH do conteúdo; cai para o ano anterior
#    se o ZIP do ano corrente ainda não existir.
#  • Retries com backoff exponencial, logging estruturado, e o Sheets
#    só reescreve uma aba se os dados realmente mudaram.
#  • /status mostra a data/hora mais recente de cada fonte de dados.
#  • Coleta de radiação solar (Open-Meteo hora a hora e diária).
# ==========================================================

import io
import json
import logging
import os
import threading
import time
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from flask import Flask, Response, request as flask_request
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

# De quantas em quantas horas o ciclo de atualização roda (Open-Meteo e
# INMET API de tempo real). O ZIP anual só entra como reserva.
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
# Planilha DIFERENTE da de destino: onde a estação física registra os
# dados. O app só LÊ dela e copia para a tabela unificada.
ESTACAO_METEO_SHEET_ID = os.environ.get(
    "ESTACAO_METEO_SHEET_ID",
    "1t2ZztZ7zBMZD148G4Ib6USTTkVVe4hWnS-CGgEd7CQM",
)
# Nome da aba a ler. Deixe em branco para usar a primeira aba.
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
    "Dados da Comunidade":               "src-comunidade",
}

# ── Dados da Comunidade (formulário público em /adicionar) ─────────
# Cada envio é gravado numa aba própria ("Dados da Comunidade") dentro
# da MESMA planilha de destino, relida a cada ciclo.
DADOS_COMUNIDADE_ABA = "Dados da Comunidade"
CAMPOS_COMUNIDADE = [
    "Nome/Apelido", "Cidade/Bairro", "Data", "Hora",
    "Temperatura (°C)", "Umidade (%RH)", "Vento (km/h)",
    "Precip. (mm)", "Radiação Solar (W/m²)", "Observação",
]

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
        f"wind_speed_10m,precipitation,weather_code,shortwave_radiation"
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
                "Radiação Solar (W/m²)": h["shortwave_radiation"][i],
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
        f"weather_code,sunrise,sunset,shortwave_radiation_sum"
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
                "Radiação Solar (MJ/m²)": d["shortwave_radiation_sum"][i],
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
    Lê os dados da planilha da estação meteorológica física e devolve
    como DataFrame, com a coluna "Estacao" preenchida.

    IMPORTANTE: essa planilha precisa estar compartilhada (Leitor ou
    Editor) com o e-mail da Service Account — senão a leitura falha.

    Se a planilha tiver coluna de radiação solar, ela entra
    automaticamente junto com as demais colunas.
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
# DADOS DA COMUNIDADE (formulário público em /adicionar)
# ==========================================================

def coletar_dados_comunidade():
    """
    Relê a aba "Dados da Comunidade" (mesma planilha de destino) e devolve
    como DataFrame, para entrar junto na tabela unificada.
    """
    if not GSHEETS_ATIVO:
        return pd.DataFrame()

    import gspread

    try:
        gc = _obter_cliente_gspread()
        sh = gc.open(GSHEETS_NOME)
        try:
            ws = sh.worksheet(DADOS_COMUNIDADE_ABA)
        except gspread.exceptions.WorksheetNotFound:
            return pd.DataFrame()
        registros = ws.get_all_records()
        if not registros:
            return pd.DataFrame()
        df = pd.DataFrame(registros)
        df.insert(0, "Estacao", "Dados da Comunidade")
        _marcar_diagnostico("Dados da Comunidade", True, len(df))
        return df
    except Exception as e:
        log.error(f"Erro ao ler dados da comunidade: {e}")
        _marcar_diagnostico("Dados da Comunidade", False, 0, str(e))
        return pd.DataFrame()

def _salvar_dados_comunidade(linhas):
    """
    Grava várias linhas de uma vez (append_rows) na aba "Dados da
    Comunidade", criando a aba com o cabeçalho se ainda não existir.

    Como qualquer CSV é aceito, o cabeçalho da aba é ajustado
    dinamicamente: colunas novas são acrescentadas ao final do cabeçalho
    existente, sem apagar nem reordenar o que já estava lá.
    """
    if not linhas:
        return
    if not GSHEETS_ATIVO:
        raise RuntimeError("Google Sheets desativado; não é possível salvar envios do formulário.")

    import gspread

    gc = _obter_cliente_gspread()
    sh = gc.open(GSHEETS_NOME)

    colunas_envio = []
    for linha in linhas:
        for col in linha.keys():
            if col not in colunas_envio:
                colunas_envio.append(col)

    try:
        ws = sh.worksheet(DADOS_COMUNIDADE_ABA)
        cabecalho_atual = ws.row_values(1)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=DADOS_COMUNIDADE_ABA, rows=1000, cols=max(len(colunas_envio), 1)
        )
        cabecalho_atual = []

    colunas_novas   = [c for c in colunas_envio if c not in cabecalho_atual]
    cabecalho_final = cabecalho_atual + colunas_novas

    if cabecalho_final != cabecalho_atual:
        ws.update([cabecalho_final])

    valores = [[str(linha.get(campo, "")) for campo in cabecalho_final] for linha in linhas]
    ws.append_rows(valores)

# ── Reconhecimento das colunas do CSV enviado pela pessoa ──────────
ALIASES_COMUNIDADE = {
    "nome": "Nome/Apelido", "apelido": "Nome/Apelido", "nome/apelido": "Nome/Apelido",
    "nome apelido": "Nome/Apelido",
    "cidade": "Cidade/Bairro", "bairro": "Cidade/Bairro", "cidade/bairro": "Cidade/Bairro",
    "cidade bairro": "Cidade/Bairro", "local": "Cidade/Bairro", "localidade": "Cidade/Bairro",
    "data": "Data",
    "hora": "Hora",
    "temperatura": "Temperatura (°C)", "temp": "Temperatura (°C)",
    "temperatura (°c)": "Temperatura (°C)", "temperatura c": "Temperatura (°C)",
    "temperatura (c)": "Temperatura (°C)",
    "umidade": "Umidade (%RH)", "umidade (%rh)": "Umidade (%RH)", "umidade %": "Umidade (%RH)",
    "vento": "Vento (km/h)", "vento (km/h)": "Vento (km/h)", "vento km/h": "Vento (km/h)",
    "precipitacao": "Precip. (mm)", "precip": "Precip. (mm)", "precip. (mm)": "Precip. (mm)",
    "chuva": "Precip. (mm)", "chuva (mm)": "Precip. (mm)",
    "radiacao": "Radiação Solar (W/m²)", "radiacao solar": "Radiação Solar (W/m²)",
    "radiacao solar (w/m2)": "Radiação Solar (W/m²)", "radiacao (w/m2)": "Radiação Solar (W/m²)",
    "observacao": "Observação", "observacoes": "Observação", "obs": "Observação",
    "comentario": "Observação", "comentarios": "Observação",
}

def _normalizar_texto(txt):
    """Minúsculas, sem acento e sem espaços nas pontas."""
    import unicodedata
    txt = str(txt).strip().lower()
    txt = "".join(c for c in unicodedata.normalize("NFKD", txt) if not unicodedata.combining(c))
    return txt

def _mapear_colunas_csv(df):
    renomear = {}
    for col in df.columns:
        chave = _normalizar_texto(col)
        if chave in ALIASES_COMUNIDADE:
            renomear[col] = ALIASES_COMUNIDADE[chave]
    return df.rename(columns=renomear)

def _processar_csv_comunidade(conteudo_bytes, nome_padrao):
    """
    Lê o CSV enviado e devolve (linhas_validas, total, ignoradas).

    ACEITA QUALQUER CSV, com qualquer conjunto de colunas. Colunas com
    nomes reconhecidos (ALIASES_COMUNIDADE) são renomeadas para o padrão;
    as demais são mantidas como vieram. Separador por vírgula ou ponto e
    vírgula (detecção automática); tenta UTF-8 e cai para Latin-1.
    """
    texto = None
    for encoding in ("utf-8-sig", "latin1"):
        try:
            texto = conteudo_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if texto is None:
        raise ValueError("Não foi possível ler a codificação do arquivo.")

    df = pd.read_csv(io.StringIO(texto), sep=None, engine="python")
    df = _mapear_colunas_csv(df)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.fillna("").astype(str)
    for col in df.columns:
        df[col] = df[col].str.strip()

    agora = datetime.now()
    total = len(df)

    if "Nome/Apelido" not in df.columns:
        df["Nome/Apelido"] = ""
    df.loc[df["Nome/Apelido"] == "", "Nome/Apelido"] = nome_padrao or "Anônimo"

    if "Data" not in df.columns:
        df["Data"] = ""
    df.loc[df["Data"] == "", "Data"] = agora.strftime("%Y-%m-%d")

    if "Hora" not in df.columns:
        df["Hora"] = ""
    df.loc[df["Hora"] == "", "Hora"] = agora.strftime("%H:%M")

    ordem_conhecida = [c for c in CAMPOS_COMUNIDADE if c in df.columns]
    extras          = [c for c in df.columns if c not in CAMPOS_COMUNIDADE]
    df = df[ordem_conhecida + extras]

    linhas_validas = []
    for _, row in df.iterrows():
        linha = row.to_dict()
        outros_valores = [v for k, v in linha.items() if k not in ("Nome/Apelido", "Data", "Hora")]
        if outros_valores and not any(v.strip() for v in outros_valores):
            continue
        linhas_validas.append(linha)

    ignoradas = total - len(linhas_validas)
    return linhas_validas, total, ignoradas

# ==========================================================
# INMET — API de tempo real (fonte principal) + ZIP anual (reserva)
# ==========================================================
#
# 1) API de tempo real (apitempo.inmet.gov.br): dado hora a hora.
# 2) ZIP histórico anual: passa por controle de qualidade (1-3 semanas
#    de atraso). Usado só como RESERVA.
#
# Ambas já trazem a coluna de radiação solar quando disponível; como o
# código só repassa as colunas devolvidas, ela aparece sem alterações.

URL_INMET_API_BASE = "https://apitempo.inmet.gov.br/estacao/{inicio}/{fim}/{codigo}"

def coletar_inmet_api():
    """
    Busca o dado hora a hora mais recente de cada estação via API de
    tempo real do INMET (últimos 2 dias, como margem de segurança).
    Retorna DataFrame vazio se a API não responder para NENHUMA estação.
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
    CONTEÚDO mudou (hash SHA-256). Só é chamado quando a API de tempo
    real falha para todas as estações.
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
    tempo real primeiro; se ela não devolver nada, cai para o ZIP.
    Retorna: (DataFrame, houve_atualizacao: bool)
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
    forcar_inmet: repassado ao caminho de reserva (ZIP anual); ignora o
    cache de hash. Sem efeito quando a API de tempo real responde.
    """
    global _ultimo_bloco_inmet

    df_hora       = coletar_om_horario()
    df_prev       = coletar_om_diario()
    df_estacao    = coletar_estacao_meteorologica()
    df_comunidade = coletar_dados_comunidade()

    df_inmet_novo, inmet_mudou = coletar_inmet(forcar=forcar_inmet)
    if inmet_mudou and not df_inmet_novo.empty:
        _ultimo_bloco_inmet = df_inmet_novo
    else:
        log.info("INMET: usando o último bloco de dados válido dessa fonte (nenhuma novidade neste ciclo).")

    df_inmet = _ultimo_bloco_inmet

    partes = [df for df in [df_hora, df_prev, df_estacao, df_comunidade, df_inmet] if not df.empty]
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

# ==========================================================
# GRÁFICOS — dados (compartilhados pelo Sheets e pela interface web)
# ==========================================================
#
# Aba "Gráficos" no Sheets: dois blocos NUMÉRICOS do Open-Meteo
#     A:B  -> temperatura hora a hora de hoje
#     D:F  -> máxima e mínima dos próximos 7 dias
# Os gráficos nativos são criados uma vez; depois só os números mudam.
# Gravados por função própria porque _escrever_aba_com_retry converte
# tudo em texto, e o Sheets não plota texto.

ABA_GRAFICOS = "Gráficos"
_GRAF_LINHAS_MAX = 200

def _dados_graficos(df):
    """Extrai da tabela unificada os dois blocos numéricos do Open-Meteo."""
    linhas_hora, linhas_dia = [], []
    if df.empty or "Estacao" not in df.columns:
        return linhas_hora, linhas_dia

    # ── Temperatura hora a hora (hoje) ──
    df_h = df[df["Estacao"] == "Open-Meteo – Hoje (horário)"].copy()
    if not df_h.empty and "Temperatura (°C)" in df_h.columns:
        df_h["_temp"] = pd.to_numeric(df_h["Temperatura (°C)"], errors="coerce")
        df_h = df_h.dropna(subset=["_temp"]).sort_values("Hora")
        linhas_hora = [
            [str(r["Hora"])[:5], float(r["_temp"])] for _, r in df_h.iterrows()
        ]

    # ── Máxima e mínima da semana (próximos 7 dias) ──
    df_d = df[df["Estacao"] == "Open-Meteo – Previsão (diária)"].copy()
    if not df_d.empty and "Temp. Máx (°C)" in df_d.columns:
        df_d["_max"] = pd.to_numeric(df_d["Temp. Máx (°C)"], errors="coerce")
        df_d["_min"] = pd.to_numeric(df_d["Temp. Mín (°C)"], errors="coerce")
        df_d = df_d.dropna(subset=["_max", "_min"]).sort_values("Data").head(7)
        linhas_dia = [
            [str(r["Data"]), float(r["_max"]), float(r["_min"])]
            for _, r in df_d.iterrows()
        ]

    return linhas_hora, linhas_dia

def _faixa_grafico(sheet_id, col_ini, col_fim):
    return {
        "sheetId": sheet_id,
        "startRowIndex": 0,
        "endRowIndex": _GRAF_LINHAS_MAX,
        "startColumnIndex": col_ini,
        "endColumnIndex": col_fim,
    }

def _spec_grafico_linha(sheet_id, titulo, titulo_x, col_dominio, colunas_series,
                        linha_ancora, coluna_ancora):
    """Monta a requisição addChart de um gráfico de linha."""
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": titulo,
                    "basicChart": {
                        "chartType": "LINE",
                        "legendPosition": "BOTTOM_LEGEND",
                        "headerCount": 1,
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": titulo_x},
                            {"position": "LEFT_AXIS", "title": "Temperatura (°C)"},
                        ],
                        "domains": [{
                            "domain": {"sourceRange": {"sources": [
                                _faixa_grafico(sheet_id, col_dominio, col_dominio + 1)
                            ]}}
                        }],
                        "series": [
                            {
                                "series": {"sourceRange": {"sources": [
                                    _faixa_grafico(sheet_id, c, c + 1)
                                ]}},
                                "targetAxis": "LEFT_AXIS",
                            }
                            for c in colunas_series
                        ],
                    },
                },
                "position": {"overlayPosition": {
                    "anchorCell": {
                        "sheetId": sheet_id,
                        "rowIndex": linha_ancora,
                        "columnIndex": coluna_ancora,
                    },
                    "widthPixels": 620,
                    "heightPixels": 340,
                }},
            }
        }
    }

def _atualizar_aba_graficos(sh, df):
    """
    Reescreve os dados da aba "Gráficos" e, se ainda não houver gráficos
    nela, cria os dois gráficos de linha. Falhas aqui não derrubam o
    restante da exportação.
    """
    import gspread

    linhas_hora, linhas_dia = _dados_graficos(df)
    if not linhas_hora and not linhas_dia:
        log.info("Sem dados do Open-Meteo neste ciclo; aba de gráficos não foi atualizada.")
        return

    try:
        try:
            ws = sh.worksheet(ABA_GRAFICOS)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=ABA_GRAFICOS, rows=_GRAF_LINHAS_MAX, cols=20)

        # Limpa só as colunas de dados (A:F), preservando os gráficos.
        ws.batch_clear([f"A1:F{_GRAF_LINHAS_MAX}"])

        if linhas_hora:
            ws.update(
                values=[["Hora", "Temperatura (°C)"]] + linhas_hora,
                range_name="A1",
                value_input_option="USER_ENTERED",
            )
        if linhas_dia:
            ws.update(
                values=[["Data", "Temp. Máx (°C)", "Temp. Mín (°C)"]] + linhas_dia,
                range_name="D1",
                value_input_option="USER_ENTERED",
            )

        metadados = sh.fetch_sheet_metadata(
            params={"fields": "sheets(properties(sheetId,title),charts(chartId))"}
        )
        ja_tem_grafico = any(
            aba.get("properties", {}).get("title") == ABA_GRAFICOS and aba.get("charts")
            for aba in metadados.get("sheets", [])
        )
        if ja_tem_grafico:
            log.info("Aba 'Gráficos' atualizada (gráficos já existentes).")
            _marcar_diagnostico("Gráficos (aba)", True, len(linhas_hora) + len(linhas_dia),
                                "dados reescritos")
            return

        requisicoes = [
            _spec_grafico_linha(
                ws.id, "Variação da temperatura hoje (hora a hora) — Open-Meteo",
                "Hora", col_dominio=0, colunas_series=[1],
                linha_ancora=0, coluna_ancora=7,
            ),
            _spec_grafico_linha(
                ws.id, "Máxima e mínima da semana — Open-Meteo",
                "Data", col_dominio=3, colunas_series=[4, 5],
                linha_ancora=19, coluna_ancora=7,
            ),
        ]
        sh.batch_update({"requests": requisicoes})
        log.info("Gráficos de temperatura criados na aba 'Gráficos'.")
        _marcar_diagnostico("Gráficos (aba)", True, len(linhas_hora) + len(linhas_dia),
                            "gráficos criados")

    except Exception as e:
        log.error(f"Erro ao montar a aba de gráficos: {e}")
        _marcar_diagnostico("Gráficos (aba)", False, 0, str(e))

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

        # A aba Diagnóstico é escrita SEMPRE: prova visualmente que o
        # ciclo rodou (tem timestamp que muda a cada execução).
        _escrever_aba_com_retry(sh, "Diagnóstico", montar_aba_diagnostico(), pular_dedup=True)

        if df.empty:
            log.warning("Tabela principal vazia; só a aba Diagnóstico foi atualizada neste ciclo.")
            return

        _escrever_aba_com_retry(sh, "Todas", df)

        # Aba com os gráficos de temperatura (dia e semana, Open-Meteo).
        _atualizar_aba_graficos(sh, df)

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

    threading.Thread(target=_atualizar_historico_inmet, daemon=True).start()

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

# ==========================================================
# INMET HISTÓRICO — médias diárias (alimenta a aba "📈 Gráficos")
# ==========================================================
# Baixa o ZIP anual do INMET no máximo 1x a cada 24 h, em thread própria,
# e guarda só a temperatura média de cada dia por estação (resumo pequeno).
# Não entra na tabela unificada nem no Sheets, então quase não pesa.

_historico_inmet    = {}     # cidade -> DataFrame[data, tmed]
_historico_hora     = None
_historico_rodando  = False
_historico_lock     = threading.Lock()

def _atualizar_historico_inmet():
    global _historico_hora, _historico_rodando
    with _historico_lock:
        if _historico_rodando:
            return
        if _historico_hora and datetime.now() - _historico_hora < timedelta(hours=24):
            return
        _historico_rodando = True
    try:
        log.info("Atualizando histórico do INMET (médias diárias)...")
        url = _resolver_url_inmet_zip(sessao_http)
        resp = sessao_http.get(url, timeout=180)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        del resp
        novo = {}
        for codigo, cidade in ESTACOES.items():
            for arq in zf.namelist():
                if codigo not in arq:
                    continue
                df = pd.read_csv(
                    zf.open(arq), sep=";", encoding="latin1", skiprows=8, decimal=",",
                    usecols=lambda c: str(c).upper().startswith("DATA")
                    or ("TEMPERATURA DO AR" in str(c).upper() and "BULBO" in str(c).upper()),
                    low_memory=False,
                )
                col_d = encontrar_coluna(df.columns, "DATA")
                col_t = encontrar_coluna(df.columns, "TEMPERATURA DO AR", "BULBO")
                if col_d is None or col_t is None:
                    continue
                t = pd.to_numeric(df[col_t], errors="coerce")
                t = t.where(t > -100)                      # -9999 = sem medição
                d = pd.to_datetime(df[col_d], errors="coerce").dt.normalize()
                g = pd.DataFrame({"data": d, "t": t}).dropna().groupby("data")["t"].agg(["mean", "count"])
                g = g[g["count"] >= 12]                    # só dias com boa cobertura
                if not g.empty:
                    novo[cidade] = pd.DataFrame({"data": g.index, "tmed": g["mean"].values})
                break
        if novo:
            with _historico_lock:
                _historico_inmet.clear()
                _historico_inmet.update(novo)
                _historico_hora = datetime.now()
            _marcar_diagnostico("INMET histórico (gráficos)", True,
                                sum(len(v) for v in novo.values()), "médias diárias")
            log.info("OK: histórico do INMET atualizado.")
        else:
            raise RuntimeError("nenhuma estação com temperatura encontrada no ZIP")
    except Exception as e:
        log.error(f"Erro ao atualizar histórico do INMET: {e}")
        _marcar_diagnostico("INMET histórico (gráficos)", False, 0, str(e))
        with _historico_lock:                              # tenta de novo em ~6 h
            _historico_hora = datetime.now() - timedelta(hours=18)
    finally:
        with _historico_lock:
            _historico_rodando = False

# ==========================================================
# GRÁFICOS SVG (gerados no servidor, sem JavaScript nem CDN)
# ==========================================================

def _svg_linha(rotulos, series, titulo):
    """
    Gráfico de linhas em SVG puro.
    series: lista de (nome, cor, valores[, tracejado]). Valores None
    quebram a linha; séries com nome vazio não aparecem na legenda.
    """
    import math
    W, H, L, R, T, B = 860, 320, 48, 20, 42, 40
    todos = [v for it in series for v in it[2] if v is not None]
    if not todos:
        return ""
    ymin, ymax = math.floor(min(todos)) - 1, math.ceil(max(todos)) + 1
    pw, ph, n = W - L - R, H - T - B, len(rotulos)

    def x(i):
        return L + (pw / 2 if n == 1 else i * pw / (n - 1))

    def y(v):
        return T + ph - (v - ymin) / (ymax - ymin) * ph

    s = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" font-family="Arial" font-size="11">',
         f'<text x="{W/2}" y="22" text-anchor="middle" font-size="14" font-weight="bold" fill="#333">{titulo}</text>']
    for k in range(5):
        v = ymin + (ymax - ymin) * k / 4
        yy = y(v)
        s.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{W-R}" y2="{yy:.1f}" stroke="#e0e0e0"/>')
        s.append(f'<text x="{L-6}" y="{yy+4:.1f}" text-anchor="end" fill="#666">{v:.0f}°</text>')
    passo = max(1, math.ceil(n / 8))
    for i, r in enumerate(rotulos):
        if i % passo == 0:
            s.append(f'<text x="{x(i):.1f}" y="{H-B+16}" text-anchor="middle" fill="#666">{r}</text>')

    for it in series:
        nome, cor, vals = it[:3]
        tracejado = len(it) > 3 and it[3]
        segmentos, atual = [], []
        for i, v in enumerate(vals):
            if v is None:
                if atual:
                    segmentos.append(atual)
                    atual = []
            else:
                atual.append((i, v))
        if atual:
            segmentos.append(atual)
        for sg in segmentos:
            pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in sg)
            extra = ' stroke-dasharray="6 4"' if tracejado else ""
            s.append(f'<polyline points="{pts}" fill="none" stroke="{cor}" '
                     f'stroke-width="{1.8 if tracejado else 2.5}"{extra}/>')
        if not tracejado:
            for i, v in enumerate(vals):
                if v is not None:
                    s.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3" fill="{cor}">'
                             f'<title>{rotulos[i]}: {v:.1f}°C</title></circle>')

    lx = L
    for it in series:
        if it[0]:
            s.append(f'<rect x="{lx}" y="{H-14}" width="12" height="4" fill="{it[1]}"/>'
                     f'<text x="{lx+16}" y="{H-9}" fill="#333">{it[0]}</text>')
            lx += 40 + 7 * len(it[0])
    s.append("</svg>")
    return "".join(s)

_CORES_INMET = {
    "Cachoeira do Sul":         "#2E7D32",
    "Santa Maria Automática":   "#F9A825",
    "Santa Maria Convencional": "#AD1457",
}

def _graficos_inmet():
    """Média diária (últimos 60 dias) com linha de tendência, e média mensal."""
    import numpy as np
    with _historico_lock:
        dados = {c: d.copy() for c, d in _historico_inmet.items()}
    if not dados:
        return ('<div class="msg-vazio">⏳ Histórico do INMET ainda carregando '
                '(é baixado 1x por dia). Atualize a página em alguns instantes.</div>')

    ultimo = max(d["data"].max() for d in dados.values())
    dias   = pd.date_range(ultimo - pd.Timedelta(days=59), ultimo)
    rot_d  = [d.strftime("%d/%m") for d in dias]
    series, resumo = [], []
    for cidade, d in dados.items():
        cor  = _CORES_INMET.get(cidade, "#555")
        ser  = d.set_index("data")["tmed"].reindex(dias)
        vals = [None if pd.isna(v) else float(v) for v in ser]
        series.append((cidade, cor, vals))
        idx = [i for i, v in enumerate(vals) if v is not None]
        if len(idx) >= 3:
            a, b = np.polyfit(idx, [vals[i] for i in idx], 1)
            series.append(("", cor, [a * i + b for i in range(len(vals))], True))
            media = float(np.mean([vals[i] for i in idx]))
            resumo.append(f"<strong>{cidade}</strong>: média {media:.1f}°C, tendência {a:+.2f}°C/dia")

    html = '<div class="graf">' + _svg_linha(
        rot_d, series, "Temperatura média diária — INMET (60 dias, tracejado = tendência)") + "</div>"
    if resumo:
        html += '<p class="resumo">' + " &nbsp;|&nbsp; ".join(resumo).replace(".", ",") + "</p>"

    meses = sorted(set().union(*[set(d["data"].dt.to_period("M")) for d in dados.values()]))
    rot_m = [f"{m.month:02d}/{str(m.year)[2:]}" for m in meses]
    series_m = []
    for cidade, d in dados.items():
        m = d.groupby(d["data"].dt.to_period("M"))["tmed"].mean()
        series_m.append((cidade, _CORES_INMET.get(cidade, "#555"),
                         [float(m[p]) if p in m.index else None for p in meses]))
    html += '<div class="graf">' + _svg_linha(
        rot_m, series_m, "Temperatura média mensal — INMET") + "</div>"
    return html

def _html_graficos(df):
    """Conteúdo da aba "📈 Gráficos": Open-Meteo (hoje e semana) + INMET (histórico)."""
    horas, dias = _dados_graficos(df)
    html = ""
    if horas:
        html += '<div class="graf">' + _svg_linha(
            [r[0] for r in horas],
            [("Temperatura (°C)", "#1565C0", [r[1] for r in horas])],
            "Temperatura hoje (hora a hora) — Open-Meteo") + "</div>"
    if dias:
        html += '<div class="graf">' + _svg_linha(
            [r[0][5:] for r in dias],
            [("Máx (°C)", "#E53935", [r[1] for r in dias]),
             ("Mín (°C)", "#1E88E5", [r[2] for r in dias])],
            "Máxima e mínima da semana — Open-Meteo") + "</div>"
    if not horas and not dias:
        html += '<div class="msg-vazio">⚠️ Open-Meteo sem dados neste momento (veja o motivo na aba do Open-Meteo).</div>'
    return html + _graficos_inmet()

@app.route("/")
def inicio():
    with tabela_lock:
        df_atual = tabela.copy()

    if df_atual.empty:
        return "<h1>Buscando os dados pela primeira vez... atualize a página em alguns segundos.</h1>"

    # As duas abas do Open-Meteo aparecem sempre, mesmo se a coleta falhou
    fixas       = ["Open-Meteo – Hoje (horário)", "Open-Meteo – Previsão (diária)"]
    fontes      = fixas + [f for f in df_atual["Estacao"].unique() if f not in fixas]
    fonte_ativa = flask_request.args.get("fonte", "todas")
    eh_graf     = fonte_ativa == "graficos"

    df_exibir = df_atual if fonte_ativa in ("todas", "graficos") else \
                df_atual[df_atual["Estacao"] == fonte_ativa]

    if eh_graf:
        conteudo = _html_graficos(df_atual)
    elif df_exibir.empty:
        with _diagnostico_lock:
            info = _diagnostico.get(fonte_ativa, {})
        motivo = info.get("detalhe") or "ainda sem coleta"
        conteudo = (f'<div class="msg-vazio">⚠️ Sem dados de <strong>{fonte_ativa}</strong> '
                    f'neste momento. Motivo: {motivo}</div>')
    else:
        if fonte_ativa == "todas":
            colunas_mostrar = list(df_atual.columns)
        else:
            colunas_mostrar = [c for c in df_atual.columns if df_exibir[c].notna().any()]
            if "Estacao" not in colunas_mostrar:
                colunas_mostrar = ["Estacao"] + colunas_mostrar
        cabecalho_html = "".join(f"<th>{col}</th>" for col in colunas_mostrar)
        linhas_html    = gerar_linhas(df_exibir[colunas_mostrar].fillna("—").head(500))
        conteudo = (f'<div class="wrapper"><table><thead><tr>{cabecalho_html}</tr></thead>'
                    f'<tbody>{linhas_html}</tbody></table></div>')

    botoes = f'<a href="/" class="btn btn-todas {"btn-ativo" if fonte_ativa=="todas" else ""}">🌐 Todas</a>\n'
    botoes += f'<a href="/?fonte=graficos" class="btn src-om-hora {"btn-ativo" if eh_graf else ""}">📈 Gráficos</a>\n'
    icones = {
        "Open-Meteo – Hoje (horário)":      "🕐",
        "Open-Meteo – Previsão (diária)":   "📅",
        "Estação Meteorológica":            "🌡️",
        "Dados da Comunidade":              "🙋",
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
    exibido = 0 if eh_graf else min(500, len(df_exibir))
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
            &nbsp;|&nbsp; veja os gráficos de temperatura na aba <strong>Gráficos</strong>.
        </div>"""

    banner_comunidade = """
    <div class="banner-add">
        🙋 Tem dados meteorológicos próprios? &nbsp;
        <a href="/adicionar"><strong>Envie um CSV com seus dados →</strong></a>
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

        .banner-add {{
            background: #EDE7F6; border: 1px solid #B39DDB;
            border-radius: 8px; padding: 10px 16px; margin-bottom: 14px;
            font-size: 13px; color: #4527A0;
        }}
        .banner-add a {{ color: #4527A0; }}

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
        .src-comunidade     {{ background: #D1C4E9; color: #4527A0; }}

        .msg-vazio {{ background: #FFF3E0; border: 1px solid #FFCC80; border-radius: 8px; padding: 12px 16px; font-size: 13px; color: #E65100; max-width: 900px; }}
        .resumo {{ font-size: 12px; color: #333; max-width: 900px; margin: -6px 0 16px; }}
        .graf {{ background: white; border-radius: 8px; padding: 12px; margin-bottom: 16px; max-width: 900px; }}

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
        tr.src-comunidade    td {{ background: #EDE7F6; }}
    </style>
</head>
<body>
    <h1>🌦️ Clima Unificado – INMET + Open-Meteo</h1>
    <p>Total de registros: <strong>{total}</strong> &nbsp;|&nbsp;
       Exibindo: <strong>{exibido}</strong> &nbsp;|&nbsp;
       Fonte: <strong>{fonte_ativa}</strong> &nbsp;|&nbsp;
       Última atualização: <strong>{rodape_atualizacao}</strong></p>

    {banner_sheets}
    {banner_comunidade}

    <div class="filtros">
        {botoes}
    </div>

    {conteudo}
</body>
</html>"""

def _data_mais_recente_por_fonte(df):
    """
    Para cada fonte, acha a data/hora mais recente nos dados. Reconhece
    as colunas do ZIP ('Data', 'Hora') e da API do INMET ('DT_MEDICAO',
    'HR_MEDICAO'). Usado só para diagnóstico no /status.
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

@app.route("/adicionar", methods=["GET", "POST"])
def adicionar_dado():
    """
    Formulário público: qualquer pessoa pode enviar um CSV com dados
    meteorológicos próprios (qualquer conjunto de colunas). As linhas
    válidas vão para a aba "Dados da Comunidade" e entram na tabela
    unificada.

    ATENÇÃO: não há login nem validação de veracidade. Os dados aparecem
    identificados como "Dados da Comunidade" (autodeclarados).
    """
    erro        = None
    sucesso_qtd = None
    ignoradas   = 0

    if flask_request.method == "POST":
        nome    = flask_request.form.get("nome", "").strip()
        arquivo = flask_request.files.get("arquivo")

        if not arquivo or arquivo.filename == "":
            erro = "Selecione um arquivo .csv para enviar."
        elif not arquivo.filename.lower().endswith(".csv"):
            erro = "O arquivo precisa ser um .csv."
        else:
            try:
                conteudo = arquivo.read()
                linhas, total, ignoradas = _processar_csv_comunidade(conteudo, nome)
                if not linhas:
                    erro = ("Nenhuma linha com dados foi encontrada nesse CSV — parece estar "
                            "vazio ou só com linhas em branco.")
                else:
                    _salvar_dados_comunidade(linhas)
                    sucesso_qtd = len(linhas)
                    # atualiza a tabela em segundo plano
                    threading.Thread(target=atualizar_dados, daemon=True).start()
            except Exception as e:
                log.error(f"Erro ao processar CSV da comunidade: {e}")
                erro = "Não foi possível ler esse arquivo. Confira o formato (CSV) e tente novamente."

    mensagem_html = ""
    if sucesso_qtd is not None:
        extra = f" ({ignoradas} linha(s) ignorada(s) por estarem vazias)" if ignoradas else ""
        mensagem_html = (
            f'<div class="msg msg-ok">✅ {sucesso_qtd} registro(s) enviado(s){extra}! '
            f'Pode levar alguns segundos para aparecer na tabela.</div>'
        )
    elif erro:
        mensagem_html = f'<div class="msg msg-erro">⚠️ {erro}</div>'

    colunas_modelo = "".join(f"<li>{c}</li>" for c in CAMPOS_COMUNIDADE)

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Adicionar dados — Clima Unificado</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f0f2f5; }}
        h1   {{ color: #4527A0; margin-bottom: 4px; }}
        p.info {{ color: #555; font-size: 13px; margin-bottom: 20px; }}
        a.voltar {{ font-size: 13px; color: #1565C0; text-decoration: none; }}
        form {{
            background: white; border-radius: 10px; padding: 20px 24px;
            max-width: 460px; box-shadow: 0 1px 4px rgba(0,0,0,.1);
        }}
        label {{ display: block; font-size: 13px; font-weight: bold; color: #333; margin: 12px 0 4px; }}
        input[type="text"], input[type="file"] {{
            width: 100%; padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px;
            font-size: 14px; box-sizing: border-box; background: white;
        }}
        button {{
            margin-top: 18px; background: #4527A0; color: white; border: none;
            padding: 10px 22px; border-radius: 20px; font-size: 14px; font-weight: bold;
            cursor: pointer;
        }}
        button:hover {{ opacity: .85; }}
        .msg {{ padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 16px; max-width: 460px; }}
        .msg-ok    {{ background: #E8F5E9; border: 1px solid #A5D6A7; color: #1B5E20; }}
        .msg-erro  {{ background: #FFEBEE; border: 1px solid #EF9A9A; color: #B71C1C; }}
        .modelo {{
            background: #F3E5F5; border: 1px solid #CE93D8; border-radius: 8px;
            padding: 12px 16px; max-width: 460px; font-size: 12px; color: #4A148C; margin-top: 18px;
        }}
        .modelo ul {{ margin: 6px 0 8px 18px; padding: 0; }}
        .modelo a {{ color: #4527A0; font-weight: bold; }}
    </style>
</head>
<body>
    <a class="voltar" href="/">← Voltar para a tabela</a>
    <h1>🙋 Adicionar dados meteorológicos</h1>
    <p class="info">Envie um arquivo CSV com seus dados (de uma planilha, estação caseira, etc.).
       Qualquer CSV é aceito — pode ter as colunas que você já tiver, com o nome que já tiver;
       não é preciso adaptar o arquivo a um formato específico. Cada linha do arquivo vira um
       registro na tabela, identificado como "Dados da Comunidade".</p>

    {mensagem_html}

    <form method="POST" action="/adicionar" enctype="multipart/form-data">
        <label>Nome ou apelido (usado se o CSV não tiver essa coluna)</label>
        <input type="text" name="nome" placeholder="Opcional">

        <label>Arquivo CSV</label>
        <input type="file" name="arquivo" accept=".csv" required>

        <button type="submit">Enviar CSV</button>
    </form>

    <div class="modelo">
        Nenhuma coluna é obrigatória — pode enviar seu CSV do jeito que ele já está.
        Se o arquivo tiver colunas parecidas com estas (sem precisar ser o nome exato — sem
        acento, "temp", "chuva" etc. também são reconhecidos), elas são identificadas
        automaticamente:
        <ul>{colunas_modelo}</ul>
        Qualquer outra coluna do arquivo é mantida do jeito que veio e também entra na tabela.
        <br><a href="/modelo-comunidade.csv">📥 Baixar modelo de CSV (opcional, só como referência)</a>
    </div>
</body>
</html>"""

@app.route("/modelo-comunidade.csv")
def modelo_comunidade_csv():
    """CSV de exemplo com o cabeçalho esperado."""
    linhas_exemplo = [
        CAMPOS_COMUNIDADE,
        ["Maria", "Cachoeira do Sul - Centro", "2026-09-02", "14:00",
         "27.5", "60", "8", "0", "650", "Céu limpo"],
    ]
    texto_csv = "\n".join(";".join(str(v) for v in linha) for linha in linhas_exemplo)
    return Response(
        texto_csv,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=modelo-dados-comunidade.csv"},
    )

@app.route("/status")
def status():
    """
    Checa se o servidor está vivo (útil para keep-alive) e mostra a
    data/hora mais recente presente em cada fonte de dados.
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
    Dispara uma atualização manual. Use /atualizar?inmet=1 apenas para
    forçar o caminho de RESERVA (ZIP anual) a ignorar o cache de hash.
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
