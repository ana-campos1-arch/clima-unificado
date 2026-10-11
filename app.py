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
# requirements.txt: flask, pandas, numpy, requests, gspread,
#                   google-auth, reportlab
#
# O QUE MUDOU NESTA VERSÃO
# ─────────────────────────────────────────────────────────
#  • NOVO: nos gráficos que comparam várias séries (INMET × Open-Meteo ×
#    Estação, as 3 cidades do INMET, hoje hora a hora), cada série agora tem
#    cor + estilo de linha + formato de marcador próprios (círculo contínuo,
#    quadrado traço-ponto, triângulo pontilhado). Vale para site, PDF e
#    gráficos nativos do Google Sheets. Gráficos de uma série só ficam como
#    estavam. Só visual: nenhuma coleta/API muda.
#  • A aba "Gráficos" do Sheets usa a marca "layout-v4": é recriada uma única
#    vez no próximo ciclo para aplicar os novos estilos.
#  • Botão "Baixar PDF dos gráficos" (rota /graficos.pdf), desenhado no
#    servidor com reportlab e guardado em cache até os dados mudarem.
#  • Gráficos de comparação HOJE, hora a hora, Open-Meteo × Estação.
#  • UMA lista de gráficos (_montar_graficos) alimenta site, PDF e Sheets.
#  • Mantido: INMET pela API de tempo real (ZIP anual só como reserva),
#    retries com backoff, Sheets só reescreve aba que mudou, /status.
# ==========================================================

import io
import logging
import math
import os
import re
import subprocess
import sys
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
    "1Vxkjf4LC5h72mAy6rtWfvfz-Bw_sx6LYkklLhIueddU",
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
        # Total de chuva do dia = soma da precipitação de todas as horas
        chuva_dia = round(sum(v or 0 for v in h["precipitation"]), 1)
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
                "Chuva do dia (mm)": chuva_dia,
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
# GOOGLE SHEETS — abas de dados
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
# HISTÓRICOS (alimentam os gráficos de comparação e do INMET)
# ==========================================================
# INMET: baixa o ZIP anual no máximo 1x a cada 24 h, em thread própria,
# e guarda só os resumos diários por estação (pequeno).
# Open-Meteo: 1 chamada leve a cada 6 h, resumida por dia.

_historico_inmet    = {}     # cidade -> DataFrame[data, tmed, umid, vento, chuva, rad]
_historico_hora     = None
_historico_rodando  = False
_historico_lock     = threading.Lock()

def _classificar_coluna_inmet(nome):
    """Diz qual variável do ZIP histórico do INMET é uma coluna (ou None)."""
    u = str(nome).upper()
    if u.startswith("DATA"):
        return "data"
    if "TEMPERATURA DO AR" in u and "BULBO" in u:
        return "tmed"
    if "UMIDADE RELATIVA DO AR" in u:
        return "umid"
    if "VENTO" in u and "VELOCIDADE" in u:
        return "vento"
    if u.startswith("PRECIPITA"):
        return "chuva"
    if u.startswith("RADIA"):
        return "rad"
    return None

def _atualizar_historico_inmet():
    """
    Guarda, por estação e por dia: temperatura média, umidade média, vento
    médio (km/h), chuva total (mm) e radiação total (MJ/m²).
    """
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
                    usecols=lambda c: _classificar_coluna_inmet(c) is not None,
                    low_memory=False,
                )
                cols = {}
                for c in df.columns:
                    cols.setdefault(_classificar_coluna_inmet(c), c)
                if "data" not in cols or "tmed" not in cols:
                    continue
                g = pd.DataFrame({
                    "data": pd.to_datetime(df[cols["data"]], errors="coerce").dt.normalize()
                })
                for k in ("tmed", "umid", "vento", "chuva", "rad"):
                    if k in cols:
                        v = pd.to_numeric(df[cols[k]], errors="coerce")
                        g[k] = v.where(v > -9000)          # -9999 = sem medição
                g = g.dropna(subset=["data"])
                grp, cnt = g.groupby("data"), g.groupby("data").count()
                res = pd.DataFrame(index=cnt.index)
                res["tmed"] = grp["tmed"].mean().where(cnt["tmed"] >= 12)
                for k, fator in (("umid", 1.0), ("vento", 3.6)):   # vento: m/s -> km/h
                    if k in g:
                        res[k] = (grp[k].mean() * fator).where(cnt[k] >= 12)
                if "chuva" in g:
                    res["chuva"] = grp["chuva"].sum(min_count=1).where(cnt["chuva"] >= 12)
                if "rad" in g:                                     # kJ/m² -> MJ/m²
                    res["rad"] = (grp["rad"].apply(lambda x: x.clip(lower=0).sum(min_count=1)) / 1000
                                  ).where(cnt["rad"] >= 12)
                res = res.dropna(subset=["tmed"]).reset_index()
                if not res.empty:
                    novo[cidade] = res
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

_om_historico      = pd.DataFrame()
_om_hist_hora      = None
_om_hist_lock      = threading.Lock()

def _atualizar_om_historico():
    global _om_historico, _om_hist_hora
    with _om_hist_lock:
        if _om_hist_hora and datetime.now() - _om_hist_hora < timedelta(hours=6):
            return
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={LATITUDE}&longitude={LONGITUDE}"
        f"&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,"
        f"precipitation,shortwave_radiation"
        f"&past_days=30&forecast_days=1&timezone=America/Sao_Paulo"
    )
    try:
        resp = sessao_http.get(url, timeout=30)
        resp.raise_for_status()
        h = resp.json()["hourly"]
        d = pd.DataFrame({
            "data":  pd.to_datetime(h["time"]).normalize(),
            "tmed":  h["temperature_2m"],
            "umid":  h["relative_humidity_2m"],
            "vento": h["wind_speed_10m"],
            "chuva": h["precipitation"],
            "rad":   h["shortwave_radiation"],
        })
        for k in ("tmed", "umid", "vento", "chuva", "rad"):
            d[k] = pd.to_numeric(d[k], errors="coerce")
        grp = d.groupby("data")
        res = pd.DataFrame({
            "tmed":  grp["tmed"].mean(),
            "umid":  grp["umid"].mean(),
            "vento": grp["vento"].mean(),
            "chuva": grp["chuva"].sum(min_count=1),
            "rad":   grp["rad"].sum(min_count=1) * 0.0036,     # W/m² por hora -> MJ/m²
        })
        with _om_hist_lock:
            _om_historico = res
            _om_hist_hora = datetime.now()
        _marcar_diagnostico("Open-Meteo histórico (30 dias)", True, len(res))
        log.info(f"OK: Open-Meteo histórico de {len(res)} dias.")
    except Exception as e:
        log.error(f"Erro ao buscar Open-Meteo histórico: {e}")
        _marcar_diagnostico("Open-Meteo histórico (30 dias)", False, 0, str(e))
        with _om_hist_lock:                                # tenta de novo em ~1 h
            _om_hist_hora = datetime.now() - timedelta(hours=5)

# ==========================================================
# ESTAÇÃO METEOROLÓGICA — resumos diário e horário (p/ gráficos)
# ==========================================================

_cache_estacao = {"stamp": None, "res": (None, {})}

def _preparar_estacao(e):
    """
    Limpa as leituras da Estação Meteorológica (1 por minuto). Reconhece
    as colunas pelo nome (sem acento/maiúscula). Devolve (g, tem_hora) com
    colunas data, h (hora) e as variáveis encontradas, ou None.
    """
    norm = {c: _normalizar_texto(c) for c in e.columns}

    def achar(tem, sem=()):
        for c, n in norm.items():
            if any(t in n for t in tem) and not any(x in n for x in sem):
                return c
        return None

    c_data  = achar(("data",))
    c_hora  = achar(("hora",))
    c_temp  = achar(("temperatura",), ("bmp", "max", "min", "orvalho"))
    c_umid  = achar(("umidade",), ("max", "min"))
    c_vento = achar(("vento",), ("direcao", "rajada"))
    c_chuva = achar(("precipita", "chuva", "pluv"))
    c_rad   = achar(("radia",))
    if c_data is None:
        return None

    def num(c):
        return pd.to_numeric(e[c].astype(str).str.replace(",", ".", regex=False), errors="coerce")

    g = pd.DataFrame({"data": pd.to_datetime(e[c_data].astype(str), dayfirst=True,
                                              errors="coerce").dt.normalize()})
    g["h"] = e[c_hora].astype(str).str.extract(r"(\d{1,2})")[0] if c_hora else "00"
    if c_temp:
        t = num(c_temp);  g["tmed"] = t.where((t > -20) & (t < 60))
    if c_umid:
        u = num(c_umid);  g["umid"] = u.where((u >= 0) & (u <= 105)).clip(upper=100)
    if c_vento:
        v = num(c_vento); v = v.where((v >= 0) & (v < 100))
        g["vento"] = v * 3.6 if "m/s" in norm[c_vento] else v
    if c_chuva:
        r = num(c_chuva); g["chuva"] = r.where((r >= 0) & (r < 50))
    if c_rad and "w/m" in norm[c_rad]:
        w = num(c_rad);   g["rad"] = w.where(w >= 0)          # W/m²
    g = g.dropna(subset=["data"])
    if g.empty:
        return None
    return g, bool(c_hora)

def _estacao_resumos(df):
    """
    Devolve (diario, horario_hoje):
      diario: DataFrame indexado por dia (tmed, umid, vento, chuva, rad em MJ/m²)
              — só dias com leituras em pelo menos 18 horas diferentes.
      horario_hoje: {variável: {"HH:00": valor}} (chuva = soma da hora;
              radiação em W/m², igual ao Open-Meteo).
    Guardado em cache até a tabela mudar.
    """
    stamp = (ultima_atualizacao, len(df))
    if _cache_estacao["stamp"] == stamp:
        return _cache_estacao["res"]
    diario, horario = None, {}
    try:
        e = df[df["Estacao"] == "Estação Meteorológica"].dropna(axis=1, how="all")
        prep = _preparar_estacao(e) if not e.empty else None
        if prep is not None:
            g, tem_hora = prep

            # ── diário ──
            grp = g.groupby("data")
            horas = grp["h"].nunique()
            res = pd.DataFrame(index=horas.index)
            for k in ("tmed", "umid", "vento"):
                if k in g:
                    res[k] = grp[k].mean()
            if "chuva" in g:
                res["chuva"] = grp["chuva"].sum(min_count=1)
            if "rad" in g:
                res["rad"] = grp["rad"].mean() * 0.0864        # W/m² médio -> MJ/m² no dia
            diario = res[horas >= (18 if tem_hora else 1)].dropna(how="all")

            # ── hoje, hora a hora ──
            gh = g[g["data"] == pd.Timestamp(date.today())]
            if tem_hora and not gh.empty:
                gh = gh.assign(hn=pd.to_numeric(gh["h"], errors="coerce")).dropna(subset=["hn"])
                gh = gh[(gh["hn"] >= 0) & (gh["hn"] <= 23)]
                gp = gh.groupby(gh["hn"].astype(int))
                for k in ("tmed", "umid", "vento", "rad", "chuva"):
                    if k in gh:
                        s = gp[k].sum(min_count=1) if k == "chuva" else gp[k].mean()
                        horario[k] = {f"{int(h):02d}:00": float(v) for h, v in s.items() if pd.notna(v)}
    except Exception as ex:
        log.error(f"Erro ao resumir a Estação Meteorológica: {ex}")
    _cache_estacao["stamp"], _cache_estacao["res"] = stamp, (diario, horario)
    return diario, horario

# ==========================================================
# GRÁFICOS — UMA lista, usada pelo site, pelo PDF e pelo Google Sheets
# ==========================================================
# Cada gráfico é um dict:
#   id, secao, titulo, sufixo (só site/PDF), rotulos, series, un, tip, zero,
#   casas, ty (título do eixo Y), tx (título do eixo X)
# series: lista de (nome, cor, valores[, tracejado]). valores com None = sem
# dado. A lista é SEMPRE a mesma (mesmos gráficos, mesmas séries, mesmo
# número de linhas); só os valores mudam. Isso mantém fixo o layout da aba
# "Gráficos" no Sheets, onde cada gráfico nativo é criado uma única vez.

SEC_OM    = "Open-Meteo — hoje e próximos dias"
SEC_HOJE  = "Hoje, hora a hora — Open-Meteo × Estação Meteorológica"
SEC_COMP  = "Últimos 30 dias — INMET × Open-Meteo × Estação Meteorológica"
SEC_INMET = "INMET — histórico das estações"
ORDEM_SECOES = [SEC_OM, SEC_HOJE, SEC_COMP, SEC_INMET]

INMET_COMPARACAO = "Cachoeira do Sul"     # estação do INMET usada na comparação de 30 dias

_HORAS = [f"{h:02d}:00" for h in range(24)]

_CORES_INMET = {
    "Cachoeira do Sul":         "#2E7D32",
    "Santa Maria Automática":   "#F9A825",
    "Santa Maria Convencional": "#AD1457",
}

# Gráficos hora a hora de hoje (Open-Meteo) além da temperatura:
# (id, coluna, título, cor, unidade eixo, unidade tooltip, eixo em 0, casas, título Y)
GRAFICOS_HORARIOS = [
    ("om_umid",  "Umidade (%RH)",         "Umidade relativa hoje (hora a hora) — Open-Meteo",
     "#00897B", "%", "%", False, 0, "Umidade (%)"),
    ("om_vento", "Vento (km/h)",          "Velocidade do vento hoje (hora a hora) — Open-Meteo",
     "#6D4C41", "", " km/h", True, 0, "Vento (km/h)"),
    ("om_rad",   "Radiação Solar (W/m²)", "Radiação solar hoje (hora a hora) — Open-Meteo",
     "#F9A825", "", " W/m²", True, 0, "Radiação (W/m²)"),
    ("om_chuva", "Precip. (mm)",          "Chuva hoje (hora a hora) — Open-Meteo",
     "#3949AB", "", " mm", True, 1, "Chuva (mm)"),
]

# Comparação de HOJE, hora a hora: Open-Meteo × Estação Meteorológica
# (coluna Open-Meteo, chave da estação, título, un, tip, zero, casas, título Y)
COMPARACAO_HOJE = [
    ("Temperatura (°C)",      "tmed",  "Temperatura hoje: Open-Meteo × Estação",     "°", "°C",    False, 0, "Temperatura (°C)"),
    ("Umidade (%RH)",         "umid",  "Umidade hoje: Open-Meteo × Estação",         "%", "%",     False, 0, "Umidade (%)"),
    ("Vento (km/h)",          "vento", "Vento hoje: Open-Meteo × Estação",           "",  " km/h", True,  0, "Vento (km/h)"),
    ("Precip. (mm)",          "chuva", "Chuva hoje (por hora): Open-Meteo × Estação","",  " mm",   True,  1, "Chuva (mm)"),
    ("Radiação Solar (W/m²)", "rad",   "Radiação hoje: Open-Meteo × Estação",        "",  " W/m²", True,  0, "Radiação (W/m²)"),
]

# Comparação dos últimos 30 dias (resumo diário)
# (chave, título, unidade no eixo, unidade no tooltip, eixo em 0, casas, título Y)
VARIAVEIS_COMPARACAO = [
    ("tmed",  "Temperatura média diária",       "°", "°C",     False, 0, "Temperatura (°C)"),
    ("umid",  "Umidade relativa média diária",  "%", "%",      False, 0, "Umidade (%)"),
    ("vento", "Velocidade média do vento",      "",  " km/h",  True,  0, "Vento (km/h)"),
    ("chuva", "Chuva total do dia",             "",  " mm",    True,  1, "Chuva (mm)"),
    ("rad",   "Radiação solar total do dia",    "",  " MJ/m²", True,  0, "Radiação (MJ/m²)"),
]

def _dados_graficos(df):
    """Extrai da tabela unificada os dois blocos numéricos do Open-Meteo (hoje e semana)."""
    linhas_hora, linhas_dia = [], []
    if df.empty or "Estacao" not in df.columns:
        return linhas_hora, linhas_dia

    df_h = df[df["Estacao"] == "Open-Meteo – Hoje (horário)"].copy()
    if not df_h.empty and "Temperatura (°C)" in df_h.columns:
        df_h["_temp"] = pd.to_numeric(df_h["Temperatura (°C)"], errors="coerce")
        df_h = df_h.dropna(subset=["_temp"]).sort_values("Hora")
        linhas_hora = [[str(r["Hora"])[:5], float(r["_temp"])] for _, r in df_h.iterrows()]

    df_d = df[df["Estacao"] == "Open-Meteo – Previsão (diária)"].copy()
    if not df_d.empty and "Temp. Máx (°C)" in df_d.columns:
        df_d["_max"] = pd.to_numeric(df_d["Temp. Máx (°C)"], errors="coerce")
        df_d["_min"] = pd.to_numeric(df_d["Temp. Mín (°C)"], errors="coerce")
        df_d = df_d.dropna(subset=["_max", "_min"]).sort_values("Data").head(7)
        linhas_dia = [[str(r["Data"]), float(r["_max"]), float(r["_min"])] for _, r in df_d.iterrows()]

    return linhas_hora, linhas_dia

def _serie_horaria(df, coluna):
    """Lista [[hora, valor], ...] de uma coluna do Open-Meteo horário."""
    if df.empty or "Estacao" not in df.columns:
        return []
    df_h = df[df["Estacao"] == "Open-Meteo – Hoje (horário)"]
    if df_h.empty or coluna not in df_h.columns:
        return []
    v = pd.to_numeric(df_h[coluna], errors="coerce")
    d = df_h.assign(_v=v).dropna(subset=["_v"]).sort_values("Hora")
    return [[str(h)[:5], float(x)] for h, x in zip(d["Hora"], d["_v"])]

def _g(id_, secao, titulo, rot, series, un="", tip="", zero=False, casas=0,
       ty="", tx="Hora", sufixo=""):
    return {"id": id_, "secao": secao, "titulo": titulo, "sufixo": sufixo,
            "rotulos": rot, "series": series, "un": un, "tip": tip,
            "zero": zero, "casas": casas, "ty": ty, "tx": tx}

def _montar_graficos(df):
    """Monta TODOS os gráficos. Devolve {"graficos": [...], "notas": {seção: [html]}}."""
    import numpy as np

    graficos, notas = [], {s: [] for s in ORDEM_SECOES}

    def v24(mapa):
        return [mapa.get(h) for h in _HORAS]

    # ── 1. Open-Meteo: hoje e semana ───────────────────────────────
    horas, dias = _dados_graficos(df)
    graficos.append(_g(
        "om_temp_hoje", SEC_OM, "Temperatura hoje (hora a hora) — Open-Meteo", _HORAS,
        [("Temperatura (°C)", "#1565C0", v24(dict(horas)))], "°", "°C", False, 0, "Temperatura (°C)"))

    falta = 7 - len(dias)
    graficos.append(_g(
        "om_semana", SEC_OM, "Máxima e mínima da semana — Open-Meteo",
        [r[0][5:] for r in dias] + [""] * falta,
        [("Máx (°C)", "#E53935", [r[1] for r in dias] + [None] * falta),
         ("Mín (°C)", "#1E88E5", [r[2] for r in dias] + [None] * falta)],
        "°", "°C", False, 0, "Temperatura (°C)", tx="Data"))

    for id_, coluna, titulo, cor, un, tip, zero, casas, ty in GRAFICOS_HORARIOS:
        mapa = dict(_serie_horaria(df, coluna))
        sufixo = ""
        if coluna == "Precip. (mm)" and mapa:   # total do dia, mesmo que seja 0
            sufixo = f" — total do dia: {sum(mapa.values()):.1f} mm".replace(".", ",")
        graficos.append(_g(id_, SEC_OM, titulo, _HORAS, [(coluna, cor, v24(mapa))],
                           un, tip, zero, casas, ty, sufixo=sufixo))

    # ── 2. Hoje, hora a hora: Open-Meteo × Estação ────────────────
    est_dia, est_hora = _estacao_resumos(df)
    for col_om, chave, titulo, un, tip, zero, casas, ty in COMPARACAO_HOJE:
        mapa_om = dict(_serie_horaria(df, col_om))
        graficos.append(_g(
            f"hoje_{chave}", SEC_HOJE, titulo, _HORAS,
            [("Open-Meteo", "#1565C0", v24(mapa_om)),
             ("Estação Meteorológica", "#E65100", v24(est_hora.get(chave, {})))],
            un, tip, zero, casas, ty))
    if not est_hora:
        notas[SEC_HOJE].append("<strong>Estação Meteorológica</strong>: sem leituras de hoje "
                               "(a linha entra sozinha quando houver dados)")
    else:
        notas[SEC_HOJE].append("Estação: média por hora das leituras (chuva = soma da hora); "
                               "Open-Meteo: valor horário do modelo")

    # ── 3. Últimos 30 dias: INMET × Open-Meteo × Estação ──────────
    dias30 = pd.date_range(end=pd.Timestamp(date.today()), periods=30)
    rot30  = [d.strftime("%d/%m") for d in dias30]
    with _historico_lock:
        d_inmet = _historico_inmet.get(INMET_COMPARACAO)
        inmet = None if d_inmet is None else d_inmet.set_index("data")
    with _om_hist_lock:
        om = _om_historico.copy()
    fontes = [
        (f"INMET {INMET_COMPARACAO}", "#2E7D32", inmet),
        ("Open-Meteo",                "#1565C0", om),
        ("Estação Meteorológica",     "#E65100", est_dia),
    ]
    usados = set()
    for chave, titulo, un, tip, zero, casas, ty in VARIAVEIS_COMPARACAO:
        series = []
        for nome, cor, d in fontes:
            if d is None or d.empty or chave not in d.columns:
                vals = [None] * 30
            else:
                vals = [None if pd.isna(v) else float(v) for v in d[chave].reindex(dias30)]
            if any(v is not None for v in vals):
                usados.add(nome)
            series.append((nome, cor, vals))
        graficos.append(_g(f"comp30_{chave}", SEC_COMP, f"{titulo} — últimos 30 dias", rot30,
                           series, un, tip, zero, casas, ty, tx="Dia"))
    for nome, _, _d in fontes:
        if nome not in usados:
            notas[SEC_COMP].append(f"<strong>{nome}</strong>: sem dados nos últimos 30 dias "
                                   f"(entra no gráfico automaticamente quando houver)")
    if est_dia is not None and not est_dia.empty:
        n_dias = int(est_dia.reindex(dias30).notna().any(axis=1).sum())
        notas[SEC_COMP].append(f"<strong>Estação Meteorológica</strong>: {n_dias} de 30 dias com dados "
                               f"(só dias com leituras em 18 h ou mais)")
    notas[SEC_COMP].append("INMET vem do histórico oficial e costuma ter 1–3 semanas de atraso")

    # ── 4. INMET: 60 dias com tendência + média mensal ────────────
    with _historico_lock:
        dados = {c: d.copy() for c, d in _historico_inmet.items()}
    ultimo = max((d["data"].max() for d in dados.values()), default=pd.Timestamp(date.today()))
    dias60 = pd.date_range(ultimo - pd.Timedelta(days=59), ultimo)
    rot60  = [d.strftime("%d/%m") for d in dias60]
    series, tendencias, resumo = [], [], []
    for cidade in ESTACOES.values():
        cor = _CORES_INMET.get(cidade, "#555")
        d = dados.get(cidade)
        if d is None:
            series.append((cidade, cor, [None] * 60))
            continue
        vals = [None if pd.isna(v) else float(v) for v in d.set_index("data")["tmed"].reindex(dias60)]
        series.append((cidade, cor, vals))
        idx = [i for i, v in enumerate(vals) if v is not None]
        if len(idx) >= 3:
            a, b = np.polyfit(idx, [vals[i] for i in idx], 1)
            tendencias.append(("", cor, [a * i + b for i in range(len(vals))], True))
            media = float(np.mean([vals[i] for i in idx]))
            resumo.append(f"<strong>{cidade}</strong>: média {media:.1f}°C, tendência {a:+.2f}°C/dia")
    graficos.append(_g("inmet_60d", SEC_INMET, "Temperatura média diária — INMET (60 dias)", rot60,
                       series + tendencias, "°", "°C", False, 0, "Temperatura (°C)", tx="Dia",
                       sufixo=", tracejado = tendência"))
    if resumo:
        notas[SEC_INMET].append(" &nbsp;|&nbsp; ".join(resumo).replace(".", ","))

    ano = int(ultimo.year)
    meses = [pd.Period(f"{ano}-{k:02d}", "M") for k in range(1, 13)]
    series_m = []
    for cidade in ESTACOES.values():
        d = dados.get(cidade)
        if d is None:
            series_m.append((cidade, _CORES_INMET.get(cidade, "#555"), [None] * 12))
            continue
        m = d.groupby(d["data"].dt.to_period("M"))["tmed"].mean()
        series_m.append((cidade, _CORES_INMET.get(cidade, "#555"),
                         [float(m[p]) if p in m.index else None for p in meses]))
    graficos.append(_g("inmet_mensal", SEC_INMET, "Temperatura média mensal — INMET",
                       [f"{k:02d}/{str(ano)[2:]}" for k in range(1, 13)], series_m,
                       "°", "°C", False, 0, "Temperatura (°C)", tx="Mês"))

    return {"graficos": graficos, "notas": notas}

_cache_graficos = {"stamp": None, "dados": None}
_cache_graficos_lock = threading.Lock()

def obter_graficos(df):
    """Monta a lista de gráficos 1x por ciclo/atualização de histórico e reaproveita."""
    stamp = (ultima_atualizacao, len(df), _historico_hora, _om_hist_hora)
    with _cache_graficos_lock:
        if _cache_graficos["stamp"] == stamp and _cache_graficos["dados"] is not None:
            return _cache_graficos["dados"]
    dados = _montar_graficos(df)
    with _cache_graficos_lock:
        _cache_graficos["stamp"], _cache_graficos["dados"] = stamp, dados
    return dados

# ==========================================================
# GRÁFICOS — preparo comum (site e PDF)
# ==========================================================

_DIM = (860, 320, 48, 20, 42, 40)       # W, H, L, R, T, B

def _titulo_completo(g):
    return g["titulo"] + g.get("sufixo", "")

def _eh_tracejada(s):
    return len(s) > 3 and bool(s[3])

def _preparar_grafico(g):
    """
    Descarta séries sem nenhum dado e corta rótulos vazios nas pontas.
    Devolve (rotulos, series) ou None se o gráfico não tem dado nenhum.
    """
    series = [s for s in g["series"] if any(v is not None for v in s[2])]
    base = [s for s in series if not _eh_tracejada(s)]
    if not base:
        return None
    n = len(g["rotulos"])
    idx = [i for i in range(n) if any(s[2][i] is not None for s in base)]
    a, b = idx[0], idx[-1] + 1
    rot = g["rotulos"][a:b]
    series = [tuple(s[:2]) + (s[2][a:b],) + tuple(s[3:]) for s in series]
    return rot, series

def _eixo_y(series, zero):
    todos = [v for it in series for v in it[2] if v is not None]
    ymin = 0 if zero else math.floor(min(todos)) - 1
    ymax = math.ceil(max(todos)) + 1
    return ymin, ymax

# ── Estilos para diferenciar séries em gráficos de comparação ──────
# (tracejado SVG/PDF, formato do marcador)
_ESTILOS = [("", "circle"), ("9 3 2 3", "square"), ("2 4", "triangle")]
# (tipo de linha, formato do ponto) no Google Sheets
_SHEETS_ESTILOS = [("SOLID", "CIRCLE"), ("LONG_DASHED_DOTTED", "SQUARE"), ("DOTTED", "TRIANGLE")]

def _estilo_serie(series, it):
    """Estilo da série; só diferencia quando há 2+ séries (comparação)."""
    base = [s for s in series if not _eh_tracejada(s)]
    if len(base) < 2 or _eh_tracejada(it):
        return _ESTILOS[0]
    k = next(i for i, s in enumerate(base) if s is it)
    return _ESTILOS[k % len(_ESTILOS)]

def _marcador_svg(forma, cx, cy, cor, tip):
    t = f"<title>{tip}</title>"
    if forma == "square":
        return f'<rect x="{cx-3:.1f}" y="{cy-3:.1f}" width="6" height="6" fill="{cor}">{t}</rect>'
    if forma == "triangle":
        return (f'<polygon points="{cx:.1f},{cy-4:.1f} {cx-4:.1f},{cy+3:.1f} {cx+4:.1f},{cy+3:.1f}" '
                f'fill="{cor}">{t}</polygon>')
    return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{cor}">{t}</circle>'

def _segmentos(vals):
    segs, atual = [], []
    for i, v in enumerate(vals):
        if v is None:
            if atual:
                segs.append(atual)
                atual = []
        else:
            atual.append((i, v))
    if atual:
        segs.append(atual)
    return segs

def _svg_linha(g):
    """Gráfico de linhas em SVG puro, a partir de um dict da lista de gráficos."""
    prep = _preparar_grafico(g)
    if prep is None:
        return ""
    rotulos, series = prep
    W, H, L, R, T, B = _DIM
    ymin, ymax = _eixo_y(series, g["zero"])
    pw, ph, n = W - L - R, H - T - B, len(rotulos)
    un, tip, casas = g["un"], g["tip"], g["casas"]

    def x(i):
        return L + (pw / 2 if n == 1 else i * pw / (n - 1))

    def y(v):
        return T + ph - (v - ymin) / (ymax - ymin) * ph

    s = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" font-family="Arial" font-size="11">',
         f'<text x="{W/2}" y="22" text-anchor="middle" font-size="14" font-weight="bold" fill="#333">'
         f'{_titulo_completo(g)}</text>']
    for k in range(5):
        v = ymin + (ymax - ymin) * k / 4
        yy = y(v)
        s.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{W-R}" y2="{yy:.1f}" stroke="#e0e0e0"/>')
        s.append(f'<text x="{L-6}" y="{yy+4:.1f}" text-anchor="end" fill="#666">{v:.{casas}f}{un}</text>')
    passo = max(1, math.ceil(n / 8))
    for i, r in enumerate(rotulos):
        if i % passo == 0:
            s.append(f'<text x="{x(i):.1f}" y="{H-B+16}" text-anchor="middle" fill="#666">{r}</text>')

    for it in series:
        cor, vals, trac = it[1], it[2], _eh_tracejada(it)
        dash, forma = _estilo_serie(series, it)
        for sg in _segmentos(vals):
            pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in sg)
            if trac:
                extra = ' stroke-dasharray="6 4"'
            else:
                extra = f' stroke-dasharray="{dash}"' if dash else ""
            s.append(f'<polyline points="{pts}" fill="none" stroke="{cor}" '
                     f'stroke-width="{1.8 if trac else 2.5}"{extra}/>')
        if not trac:
            for i, v in enumerate(vals):
                if v is not None:
                    s.append(_marcador_svg(forma, x(i), y(v), cor,
                                           f'{rotulos[i]}: {v:.{max(casas, 1)}f}{tip}'))

    lx = L
    for it in series:
        if it[0]:
            dash, _f = _estilo_serie(series, it)
            da = f' stroke-dasharray="{dash}"' if dash else ""
            s.append(f'<line x1="{lx}" y1="{H-12}" x2="{lx+22}" y2="{H-12}" '
                     f'stroke="{it[1]}" stroke-width="2.5"{da}/>'
                     f'<text x="{lx+27}" y="{H-9}" fill="#333">{it[0]}</text>')
            lx += 50 + 7 * len(it[0])
    s.append("</svg>")
    return "".join(s)

# ==========================================================
# GRÁFICOS NO GOOGLE SHEETS (aba "Gráficos", gráficos nativos)
# ==========================================================
# Layout: gráficos à ESQUERDA (grade de 2 por linha); dados à DIREITA, a
# partir da coluna AE. A coluna AD, linha 1, guarda a marca do layout: se
# for diferente (aba antiga), a aba é recriada uma única vez.

ABA_GRAFICOS        = "Gráficos"
_GRAF_MARCA         = "layout-v4"
_GRAF_MARCA_COL     = 29            # coluna AD (índice 0-based)
_GRAF_COL_INI       = 30            # coluna AE: primeiro bloco de dados
_GRAF_PASSO_LINHAS  = 18            # altura de cada gráfico, em linhas
_GRAF_PASSO_COLUNAS = 8             # segundo gráfico da linha começa na coluna 8

def _layout_sheets(graficos):
    """[(gráfico, coluna_dos_rótulos, séries_sem_tracejado)], coluna_final."""
    col, saida = _GRAF_COL_INI, []
    for g in graficos:
        sers = [s for s in g["series"] if not _eh_tracejada(s)]
        saida.append((g, col, sers))
        col += len(sers) + 2                 # rótulo + séries + 1 coluna de respiro
    return saida, col

def _faixa_grafico(sheet_id, col_ini, col_fim, linhas):
    return {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": linhas,
            "startColumnIndex": col_ini, "endColumnIndex": col_fim}

def _hex_rgb(h):
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}

def _spec_grafico_linha(sheet_id, g, col, sers, posicao):
    """Requisição addChart de um gráfico de linha nativo."""
    linhas = len(g["rotulos"]) + 1
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": g["titulo"],
                    "basicChart": {
                        "chartType": "LINE",
                        "legendPosition": "BOTTOM_LEGEND",
                        "headerCount": 1,
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": g["tx"]},
                            {"position": "LEFT_AXIS", "title": g["ty"]},
                        ],
                        "domains": [{"domain": {"sourceRange": {"sources": [
                            _faixa_grafico(sheet_id, col, col + 1, linhas)]}}}],
                        "series": [
                            {"series": {"sourceRange": {"sources": [
                                _faixa_grafico(sheet_id, col + 1 + j, col + 2 + j, linhas)]}},
                             "targetAxis": "LEFT_AXIS",
                             "color": _hex_rgb(s[1]),
                             **({"lineStyle": {"type": _SHEETS_ESTILOS[j % 3][0], "width": 3},
                                 "pointStyle": {"shape": _SHEETS_ESTILOS[j % 3][1], "size": 6}}
                                if len(sers) > 1 else {})}
                            for j, s in enumerate(sers)
                        ],
                    },
                },
                "position": {"overlayPosition": {
                    "anchorCell": {
                        "sheetId": sheet_id,
                        "rowIndex": (posicao // 2) * _GRAF_PASSO_LINHAS,
                        "columnIndex": (posicao % 2) * _GRAF_PASSO_COLUNAS,
                    },
                    "widthPixels": 620,
                    "heightPixels": 340,
                }},
            }
        }
    }

def _atualizar_aba_graficos(sh, dados):
    """
    Reescreve os dados da aba "Gráficos" (uma única chamada) e cria os
    gráficos nativos que ainda não existem (só os que já têm dados).
    Falhas aqui não derrubam o restante da exportação.
    """
    import gspread
    from gspread.utils import rowcol_to_a1

    graficos = dados["graficos"]
    layout, col_final = _layout_sheets(graficos)
    if not any(any(v is not None for s in sers for v in s[2]) for _, _, sers in layout):
        log.info("Sem dados para os gráficos neste ciclo; aba de gráficos não foi atualizada.")
        return

    try:
        largura  = col_final - _GRAF_MARCA_COL            # inclui a coluna da marca
        altura   = 1 + max(len(g["rotulos"]) for g in graficos)
        linhas_ws = max(200, (len(graficos) // 2 + 1) * _GRAF_PASSO_LINHAS + 5)

        try:
            ws = sh.worksheet(ABA_GRAFICOS)
            if ws.cell(1, _GRAF_MARCA_COL + 1).value != _GRAF_MARCA:
                log.info("Aba 'Gráficos' com layout antigo; recriando.")
                sh.del_worksheet(ws)
                raise gspread.exceptions.WorksheetNotFound
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=ABA_GRAFICOS, rows=linhas_ws, cols=col_final + 2)
        if ws.col_count < col_final + 2 or ws.row_count < linhas_ws:
            ws.resize(rows=max(ws.row_count, linhas_ws), cols=max(ws.col_count, col_final + 2))

        # Matriz única com todos os blocos (marca + dados)
        matriz = [[""] * largura for _ in range(altura)]
        matriz[0][0] = _GRAF_MARCA
        for g, col, sers in layout:
            c = col - _GRAF_MARCA_COL
            matriz[0][c] = g["tx"]
            for j, s in enumerate(sers):
                matriz[0][c + 1 + j] = s[0]
            for i, r in enumerate(g["rotulos"]):
                matriz[i + 1][c] = r
                for j, s in enumerate(sers):
                    v = s[2][i]
                    matriz[i + 1][c + 1 + j] = "" if v is None else float(v)

        # Limpa só a área de dados (à direita), preservando os gráficos
        ws.batch_clear([f"{rowcol_to_a1(1, _GRAF_MARCA_COL + 1)}:{rowcol_to_a1(ws.row_count, ws.col_count)}"])
        ws.update(values=matriz, range_name=rowcol_to_a1(1, _GRAF_MARCA_COL + 1),
                  value_input_option="RAW")   # RAW: rótulos como "14:00" ou "07/10" ficam como texto

        # Cria só os gráficos que ainda não existem e já têm dados
        meta = sh.fetch_sheet_metadata(
            params={"fields": "sheets(properties(title),charts(spec(title)))"})
        existentes = set()
        for aba in meta.get("sheets", []):
            if aba.get("properties", {}).get("title") == ABA_GRAFICOS:
                for ch in aba.get("charts", []):
                    existentes.add(ch.get("spec", {}).get("title", ""))
        novos = []
        for pos, (g, col, sers) in enumerate(layout):
            if g["titulo"] in existentes:
                continue
            if not any(v is not None for s in sers for v in s[2]):
                continue
            novos.append(_spec_grafico_linha(ws.id, g, col, sers, pos))

        if novos:
            sh.batch_update({"requests": novos})
            log.info(f"{len(novos)} gráfico(s) criado(s) na aba 'Gráficos'.")
        else:
            log.info("Aba 'Gráficos' atualizada (gráficos já existentes).")
        _marcar_diagnostico("Gráficos (aba)", True, len(graficos), f"{len(novos)} gráfico(s) criado(s)")

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

        if df.empty:
            log.warning("Tabela principal vazia; nada para exportar neste ciclo.")
            return

        _escrever_aba_com_retry(sh, "Todas", df)

        # Aba com TODOS os gráficos (mesma lista do site e do PDF)
        _atualizar_aba_graficos(sh, obter_graficos(df))

        for fonte in df["Estacao"].unique():
            df_f = df[df["Estacao"] == fonte]
            cols = [c for c in df.columns if df_f[c].notna().any()]
            _escrever_aba_com_retry(sh, fonte[:100], df_f[cols])

        log.info(f"Google Sheets sincronizado: '{GSHEETS_NOME}'")
    except Exception as e:
        log.error(f"Erro ao exportar para Google Sheets: {e}")

# ==========================================================
# PDF DOS GRÁFICOS (reportlab, desenhado no servidor)
# ==========================================================

_cache_pdf = {"dados": None, "bytes": None}
_cache_pdf_lock = threading.Lock()

def _garantir_reportlab():
    """
    Se o reportlab não estiver instalado no servidor, tenta instalar sozinho
    (pip) na primeira vez que o PDF é pedido. O ideal continua sendo ter
    'reportlab' no requirements.txt; isto é só uma rede de segurança.
    """
    try:
        import reportlab  # noqa: F401
        return
    except ImportError:
        pass
    log.warning("reportlab não encontrado; tentando instalar automaticamente...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "reportlab"],
        check=True, timeout=240,
    )
    import importlib
    importlib.invalidate_caches()
    import reportlab  # noqa: F401  (se ainda falhar, o ImportError sobe para a rota)
    log.info("reportlab instalado com sucesso.")

def _desenho_pdf(g, largura):
    """Desenha um gráfico como Drawing do reportlab (mesma geometria do SVG)."""
    from reportlab.graphics.shapes import Circle, Drawing, Line, PolyLine, Polygon, Rect, String
    from reportlab.lib import colors

    prep = _preparar_grafico(g)
    if prep is None:
        return None
    rotulos, series = prep
    W, H, L, R, T, B = _DIM
    ymin, ymax = _eixo_y(series, g["zero"])
    pw, ph, n = W - L - R, H - T - B, len(rotulos)
    un, casas = g["un"], g["casas"]
    cinza, escuro = colors.HexColor("#666666"), colors.HexColor("#333333")

    def x(i):
        return L + (pw / 2 if n == 1 else i * pw / (n - 1))

    def y(v):                                    # eixo Y do PDF é de baixo para cima
        return H - (T + ph - (v - ymin) / (ymax - ymin) * ph)

    d = Drawing(W, H)
    d.add(String(W / 2, H - 22, _titulo_completo(g), textAnchor="middle", fontSize=15,
                 fontName="Helvetica-Bold", fillColor=escuro))
    for k in range(5):
        v = ymin + (ymax - ymin) * k / 4
        yy = y(v)
        d.add(Line(L, yy, W - R, yy, strokeColor=colors.HexColor("#E0E0E0"), strokeWidth=1))
        d.add(String(L - 6, yy - 4, f"{v:.{casas}f}{un}", textAnchor="end", fontSize=12,
                     fontName="Helvetica", fillColor=cinza))
    passo = max(1, math.ceil(n / 8))
    for i, r in enumerate(rotulos):
        if i % passo == 0:
            d.add(String(x(i), B - 16, str(r), textAnchor="middle", fontSize=12,
                         fontName="Helvetica", fillColor=cinza))

    for it in series:
        cor, vals, trac = colors.HexColor(it[1]), it[2], _eh_tracejada(it)
        dash, forma = _estilo_serie(series, it)
        for sg in _segmentos(vals):
            if len(sg) < 2:
                continue
            pts = []
            for i, v in sg:
                pts += [x(i), y(v)]
            linha = PolyLine(pts, strokeColor=cor, strokeWidth=1.8 if trac else 2.5)
            if trac:
                linha.strokeDashArray = [6, 4]
            elif dash:
                linha.strokeDashArray = [float(n_) for n_ in dash.split()]
            d.add(linha)
        if not trac:
            for i, v in enumerate(vals):
                if v is None:
                    continue
                cx, cy = x(i), y(v)
                if forma == "square":
                    d.add(Rect(cx - 3, cy - 3, 6, 6, fillColor=cor, strokeColor=cor))
                elif forma == "triangle":
                    d.add(Polygon([cx, cy + 4, cx - 4, cy - 3, cx + 4, cy - 3],
                                  fillColor=cor, strokeColor=cor))
                else:
                    d.add(Circle(cx, cy, 3, fillColor=cor, strokeColor=cor))

    lx = L
    for it in series:
        if it[0]:
            dash, _f = _estilo_serie(series, it)
            ln = Line(lx, 12, lx + 22, 12, strokeColor=colors.HexColor(it[1]), strokeWidth=2.5)
            if dash:
                ln.strokeDashArray = [float(n_) for n_ in dash.split()]
            d.add(ln)
            d.add(String(lx + 27, 8, it[0], fontSize=12, fontName="Helvetica", fillColor=escuro))
            lx += 50 + 7 * len(it[0])

    esc = largura / W
    d.width, d.height = W * esc, H * esc
    d.scale(esc, esc)
    return d

def _gerar_pdf_graficos(dados):
    """PDF com todos os gráficos, por seção. Em cache até a lista de gráficos mudar."""
    with _cache_pdf_lock:
        if _cache_pdf["dados"] is dados and _cache_pdf["bytes"]:
            return _cache_pdf["bytes"]

    _garantir_reportlab()
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    estilos = getSampleStyleSheet()
    margem = 30
    largura = A4[0] - 2 * margem
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margem, rightMargin=margem,
                            topMargin=margem, bottomMargin=margem,
                            title="Clima Unificado — Gráficos")

    agora = (ultima_atualizacao or datetime.now()).strftime("%d/%m/%Y às %H:%M")
    historia = [
        Paragraph("Clima Unificado — Gráficos", estilos["Title"]),
        Paragraph(f"Dados atualizados em {agora}. Fontes: Open-Meteo, INMET e Estação Meteorológica.",
                  estilos["Normal"]),
        Spacer(1, 8),
    ]
    for secao in ORDEM_SECOES:
        desenhos = []
        for g in dados["graficos"]:
            if g["secao"] == secao:
                dr = _desenho_pdf(g, largura)
                if dr is not None:
                    desenhos.append(dr)
        if not desenhos:
            continue
        historia.append(Paragraph(secao, estilos["Heading2"]))
        for nota in dados["notas"].get(secao, []):
            historia.append(Paragraph(re.sub(r"<(?!/?strong>)[^>]+>", "", nota).replace("&nbsp;", " "),
                                      estilos["Italic"]))
        historia.append(Spacer(1, 4))
        for dr in desenhos:
            historia.append(dr)
            historia.append(Spacer(1, 8))
    doc.build(historia)

    pdf = buf.getvalue()
    with _cache_pdf_lock:
        _cache_pdf["dados"], _cache_pdf["bytes"] = dados, pdf
    return pdf

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

    # Open-Meteo histórico é leve (1 chamada a cada 6 h): roda aqui para já
    # entrar nos gráficos do Sheets. O ZIP do INMET (pesado, 1x/dia) fica em
    # segundo plano e aparece no ciclo seguinte.
    try:
        _atualizar_om_historico()
    except Exception as e:
        log.error(f"Erro no histórico do Open-Meteo: {e}")
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

def _html_graficos(df):
    """Conteúdo da aba "📈 Gráficos": botão de PDF + todas as seções."""
    dados = obter_graficos(df)
    html = ('<p><a class="btn-pdf" href="/graficos.pdf">📄 Baixar PDF dos gráficos</a></p>')
    vazios = {
        SEC_OM:    '⚠️ Open-Meteo sem dados neste momento (veja o motivo na aba do Open-Meteo).',
        SEC_COMP:  '⏳ Dados de comparação ainda carregando (histórico é buscado em segundo plano). '
                   'Atualize a página em instantes.',
        SEC_INMET: '⏳ Histórico do INMET ainda carregando (é baixado 1x por dia). '
                   'Atualize a página em alguns instantes.',
    }
    for secao in ORDEM_SECOES:
        svgs = [_svg_linha(g) for g in dados["graficos"] if g["secao"] == secao]
        svgs = [s for s in svgs if s]
        if not svgs:
            if secao in vazios:
                html += f'<h2 class="sec">{secao}</h2><div class="msg-vazio">{vazios[secao]}</div>'
            continue
        html += f'<h2 class="sec">{secao}</h2>'
        if dados["notas"].get(secao):
            html += '<p class="resumo">' + " &nbsp;|&nbsp; ".join(dados["notas"][secao]) + "</p>"
        html += "".join(f'<div class="graf">{s}</div>' for s in svgs)
    return html

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
            &nbsp;|&nbsp; os mesmos gráficos do site estão na aba <strong>Gráficos</strong>.
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

        .btn-pdf {{
            display: inline-block; background: #C62828; color: white; text-decoration: none;
            padding: 9px 20px; border-radius: 20px; font-size: 13px; font-weight: bold;
        }}
        .btn-pdf:hover {{ opacity: .85; }}

        .src-om-hora        {{ background: #B3E5FC; color: #01579B; }}
        .src-om-prev        {{ background: #C5CAE9; color: #1A237E; }}
        .src-estacao-meteo  {{ background: #FFE0B2; color: #E65100; }}
        .src-cachoeira      {{ background: #C8E6C9; color: #1B5E20; }}
        .src-sm-auto        {{ background: #FFF9C4; color: #E65100; }}
        .src-sm-conv        {{ background: #F8BBD0; color: #880E4F; }}
        .src-comunidade     {{ background: #D1C4E9; color: #4527A0; }}

        .msg-vazio {{ background: #FFF3E0; border: 1px solid #FFCC80; border-radius: 8px; padding: 12px 16px; font-size: 13px; color: #E65100; max-width: 900px; }}
        .sec {{ color: #1565C0; font-size: 17px; margin: 22px 0 6px; max-width: 900px; }}
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

@app.route("/graficos.pdf")
def baixar_pdf_graficos():
    """Baixa um PDF com todos os gráficos do site."""
    with tabela_lock:
        df_atual = tabela.copy()
    if df_atual.empty:
        return Response("Os dados ainda estão sendo buscados. Tente de novo em alguns segundos.",
                        status=503, mimetype="text/plain; charset=utf-8")
    try:
        pdf = _gerar_pdf_graficos(obter_graficos(df_atual))
    except (ImportError, subprocess.SubprocessError) as e_import:
        log.error(f"reportlab indisponível ({e_import}); adicione 'reportlab' ao requirements.txt.")
        return Response("PDF indisponível: falta instalar a biblioteca reportlab no servidor.",
                        status=500, mimetype="text/plain; charset=utf-8")
    except Exception as e:
        log.error(f"Erro ao gerar o PDF dos gráficos: {e}")
        return Response("Não foi possível gerar o PDF agora.", status=500,
                        mimetype="text/plain; charset=utf-8")
    nome = f"graficos-clima-{date.today().isoformat()}.pdf"
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={nome}"})

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
