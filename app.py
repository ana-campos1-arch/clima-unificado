# ==========================================================
# CLIMA UNIFICADO - INMET + OPEN-METEO (versão enxuta p/ Render free)
#
# Fontes: Open-Meteo, INMET (API tempo real), Estação Meteorológica
# (planilha externa) e Dados da Comunidade (CSV em /adicionar).
# Destino: Google Sheets (abas Diagnóstico, Todas, Gráficos e por fonte).
#
# Economia de recursos nesta versão:
# - Sem ZIP histórico do INMET (era o que estourava a memória).
# - Planilhas externas limitadas às últimas LIMITE_LINHAS linhas.
# - Tabela em memória sempre pequena.
# - Dados da comunidade escapados (html.escape) ao exibir.
# ==========================================================

import html
import io
import logging
import os
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from flask import Flask, Response, request as flask_request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
)
log = logging.getLogger("clima")

# ==========================================================
# CONFIGURAÇÕES
# ==========================================================

LATITUDE = -30.0397
LONGITUDE = -52.8930

ESTACOES = {
    "B822": "Cachoeira do Sul",
    "A803": "Santa Maria Automática",
    "83936": "Santa Maria Convencional",
}

INTERVALO_ATUALIZACAO_HORAS = 1
LIMITE_LINHAS = 1500  # máx. de linhas lidas das planilhas externas

GSHEETS_ATIVO = True
GSHEETS_CREDENCIAIS = os.environ.get("GSHEETS_CREDENCIAIS_PATH", "/etc/secrets/credenciais.json")
GSHEETS_NOME = os.environ.get("GSHEETS_NOME", "Clima Unificado")
GSHEETS_LINK = os.environ.get(
    "GSHEETS_LINK",
    "https://docs.google.com/spreadsheets/d/1yDFMkt0-Buuijc1LwVc6Sj8Zixk74cc0izi0azvT5sA/edit?gid=192990081#gid=192990081",
)

ESTACAO_METEO_SHEET_ID = os.environ.get(
    "ESTACAO_METEO_SHEET_ID",
    "1t2ZztZ7zBMZD148G4Ib6USTTkVVe4hWnS-CGgEd7CQM",
)
ESTACAO_METEO_ABA = os.environ.get("ESTACAO_METEO_ABA", "")

DADOS_COMUNIDADE_ABA = "Dados da Comunidade"
CAMPOS_COMUNIDADE = [
    "Nome/Apelido", "Cidade/Bairro", "Data", "Hora",
    "Temperatura (°C)", "Umidade (%RH)", "Vento (km/h)",
    "Precip. (mm)", "Radiação Solar (W/m²)", "Observação",
]

FONTE_CSS = {
    "Open-Meteo – Hoje (horário)": "src-om-hora",
    "Open-Meteo – Previsão (diária)": "src-om-prev",
    "INMET - Cachoeira do Sul": "src-cachoeira",
    "INMET - Santa Maria Automática": "src-sm-auto",
    "INMET - Santa Maria Convencional": "src-sm-conv",
    "Estação Meteorológica": "src-estacao-meteo",
    "Dados da Comunidade": "src-comunidade",
}

ICONES = {
    "Open-Meteo – Hoje (horário)": "🕐",
    "Open-Meteo – Previsão (diária)": "📅",
    "Estação Meteorológica": "🌡️",
    "Dados da Comunidade": "🙋",
    "INMET - Cachoeira do Sul": "📡",
    "INMET - Santa Maria Automática": "📡",
    "INMET - Santa Maria Convencional": "📡",
}

# ==========================================================
# HTTP COM RETRY
# ==========================================================


def criar_sessao():
    sessao = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adaptador = HTTPAdapter(max_retries=retry)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    return sessao


sessao_http = criar_sessao()

# ==========================================================
# DIAGNÓSTICO
# ==========================================================

_diagnostico_lock = threading.Lock()
_diagnostico = {}


def _marcar_diagnostico(fonte, ok, registros=0, detalhe=""):
    with _diagnostico_lock:
        _diagnostico[fonte] = {
            "ok": ok,
            "registros": registros,
            "detalhe": detalhe,
            "hora": datetime.now(),
        }


def montar_aba_diagnostico():
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
# OPEN-METEO
# ==========================================================


def coletar_om_horario():
    fonte = "Open-Meteo – Hoje (horário)"
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
        h = sessao_http.get(url, timeout=30).json()["hourly"]
        linhas = []
        for i, hora in enumerate(h["time"]):
            linhas.append({
                "Estacao": fonte,
                "Data": hora[:10],
                "Hora": hora[11:] + ":00",
                "Temperatura (°C)": h["temperature_2m"][i],
                "Umidade (%RH)": h["relative_humidity_2m"][i],
                "Vento (km/h)": h["wind_speed_10m"][i],
                "Precip. (mm)": h["precipitation"][i],
                "Cód. Clima": h["weather_code"][i],
                "Radiação Solar (W/m²)": h["shortwave_radiation"][i],
            })
        df = pd.DataFrame(linhas)
        _marcar_diagnostico(fonte, True, len(df))
        return df
    except Exception as e:
        log.error(f"Erro Open-Meteo horário: {e}")
        _marcar_diagnostico(fonte, False, 0, str(e))
        return pd.DataFrame()


def coletar_om_diario():
    fonte = "Open-Meteo – Previsão (diária)"
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
        d = sessao_http.get(url, timeout=30).json()["daily"]
        linhas = []
        for i, dia in enumerate(d["time"]):
            linhas.append({
                "Estacao": fonte,
                "Data": dia,
                "Hora": "—",
                "Temp. Máx (°C)": d["temperature_2m_max"][i],
                "Temp. Mín (°C)": d["temperature_2m_min"][i],
                "Precip. (mm)": d["precipitation_sum"][i],
                "Vento Máx (km/h)": d["wind_speed_10m_max"][i],
                "Cód. Clima": d["weather_code"][i],
                "Nascer do Sol": d["sunrise"][i],
                "Pôr do Sol": d["sunset"][i],
                "Radiação Solar (MJ/m²)": d["shortwave_radiation_sum"][i],
            })
        df = pd.DataFrame(linhas)
        _marcar_diagnostico(fonte, True, len(df))
        return df
    except Exception as e:
        log.error(f"Erro Open-Meteo diário: {e}")
        _marcar_diagnostico(fonte, False, 0, str(e))
        return pd.DataFrame()

# ==========================================================
# GSPREAD
# ==========================================================

_gspread_cliente_cache = None


def _obter_cliente_gspread():
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
# ESTAÇÃO METEOROLÓGICA (planilha externa)
# ==========================================================


def coletar_estacao_meteorologica():
    if not ESTACAO_METEO_SHEET_ID:
        return pd.DataFrame()
    fonte = "Estação Meteorológica"
    try:
        gc = _obter_cliente_gspread()
        sh = gc.open_by_key(ESTACAO_METEO_SHEET_ID)
        ws = sh.worksheet(ESTACAO_METEO_ABA) if ESTACAO_METEO_ABA else sh.sheet1
        registros = ws.get_all_records()
        if not registros:
            log.warning("Planilha da Estação Meteorológica está vazia.")
            return pd.DataFrame()
        df = pd.DataFrame(registros[-LIMITE_LINHAS:])
        del registros
        df.insert(0, "Estacao", fonte)
        _marcar_diagnostico(fonte, True, len(df))
        return df
    except Exception as e:
        log.error(f"Erro ao ler a planilha da Estação Meteorológica: {e}")
        _marcar_diagnostico(fonte, False, 0, str(e))
        return pd.DataFrame()

# ==========================================================
# DADOS DA COMUNIDADE
# ==========================================================


def coletar_dados_comunidade():
    if not GSHEETS_ATIVO:
        return pd.DataFrame()

    import gspread

    fonte = "Dados da Comunidade"
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
        df = pd.DataFrame(registros[-LIMITE_LINHAS:])
        del registros
        df.insert(0, "Estacao", fonte)
        _marcar_diagnostico(fonte, True, len(df))
        return df
    except Exception as e:
        log.error(f"Erro ao ler dados da comunidade: {e}")
        _marcar_diagnostico(fonte, False, 0, str(e))
        return pd.DataFrame()


def _salvar_dados_comunidade(linhas):
    if not linhas:
        return
    if not GSHEETS_ATIVO:
        raise RuntimeError("Google Sheets desativado.")

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

    cabecalho_final = cabecalho_atual + [c for c in colunas_envio if c not in cabecalho_atual]
    if cabecalho_final != cabecalho_atual:
        ws.update([cabecalho_final])

    valores = [[str(linha.get(campo, "")) for campo in cabecalho_final] for linha in linhas]
    ws.append_rows(valores)


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
    txt = str(txt).strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", txt) if not unicodedata.combining(c))


def _mapear_colunas_csv(df):
    renomear = {}
    for col in df.columns:
        chave = _normalizar_texto(col)
        if chave in ALIASES_COMUNIDADE:
            renomear[col] = ALIASES_COMUNIDADE[chave]
    return df.rename(columns=renomear)


def _processar_csv_comunidade(conteudo_bytes, nome_padrao):
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
    extras = [c for c in df.columns if c not in CAMPOS_COMUNIDADE]
    df = df[ordem_conhecida + extras]

    linhas_validas = []
    for _, row in df.iterrows():
        linha = row.to_dict()
        outros = [v for k, v in linha.items() if k not in ("Nome/Apelido", "Data", "Hora")]
        if outros and not any(v.strip() for v in outros):
            continue
        linhas_validas.append(linha)

    return linhas_validas, total, total - len(linhas_validas)

# ==========================================================
# INMET (API de tempo real)
# ==========================================================

URL_INMET_API_BASE = "https://apitempo.inmet.gov.br/estacao/{inicio}/{fim}/{codigo}"


def coletar_inmet():
    hoje = date.today()
    inicio = (hoje - timedelta(days=2)).isoformat()
    fim = hoje.isoformat()

    blocos = []
    for codigo, cidade in ESTACOES.items():
        url = URL_INMET_API_BASE.format(inicio=inicio, fim=fim, codigo=codigo)
        fonte_diag = f"INMET (API) - {cidade}"
        try:
            resp = sessao_http.get(url, timeout=30)
            resp.raise_for_status()
            registros = resp.json()
            if not registros or not isinstance(registros, list):
                _marcar_diagnostico(fonte_diag, False, 0, "resposta vazia")
                continue
            df = pd.DataFrame(registros)
            df.insert(0, "Estacao", f"INMET - {cidade}")
            blocos.append(df)
            _marcar_diagnostico(fonte_diag, True, len(df))
        except Exception as e:
            log.warning(f"Erro API INMET {cidade} ({codigo}): {e}")
            _marcar_diagnostico(fonte_diag, False, 0, str(e))

    if not blocos:
        return pd.DataFrame()
    return pd.concat(blocos, ignore_index=True)


def encontrar_coluna(colunas, *chaves):
    for col in colunas:
        nome = str(col).upper()
        if all(chave.upper() in nome for chave in chaves):
            return col
    return None

# ==========================================================
# ANÁLISE PARA /graficos
# ==========================================================

METRICAS_HISTORICAS = {
    "temperatura": {
        "titulo": "Temperatura", "unidade": "°C",
        "candidatos_col": [("TEMPERATURA",), ("TEM", "INS"), ("TEM", "MED"),
                           ("TEMP", "MEDIA"), ("TEMP", "MÉDIA"), ("TEMP", "MAX")],
    },
    "umidade": {
        "titulo": "Umidade Relativa", "unidade": "%",
        "candidatos_col": [("UMIDADE",), ("UMD", "INS"), ("UMD", "MED"), ("UMID", "MEDIA")],
    },
    "radiacao": {
        "titulo": "Radiação Solar", "unidade": "W/m²",
        "candidatos_col": [("RADIAÇÃO",), ("RADIACAO",), ("RAD", "GLO"), ("RADGLO", "MEDIA")],
    },
}

_CANDIDATOS_COL_DATA = [("DATA", "HORA"), ("DT", "MEDICAO"), ("DATA",)]


def _achar_coluna(df, candidatos):
    for chaves in candidatos:
        col = encontrar_coluna(df.columns, *chaves)
        if col is not None:
            return col
    return None


def extrair_serie_metrica(df_fonte, metrica, limite_pontos=60):
    info = METRICAS_HISTORICAS[metrica]
    colunas_com_dado = [c for c in df_fonte.columns if df_fonte[c].notna().any()]
    df_fonte = df_fonte[colunas_com_dado]

    col_valor = _achar_coluna(df_fonte, info["candidatos_col"])
    col_data = _achar_coluna(df_fonte, _CANDIDATOS_COL_DATA)
    if col_valor is None or col_data is None:
        return []

    df = df_fonte[[col_data, col_valor]].copy()
    df.columns = ["data", "valor"]

    col_hora = encontrar_coluna(df_fonte.columns, "HORA") or encontrar_coluna(df_fonte.columns, "HR", "MEDICAO")
    if col_hora is not None and col_hora != col_data and "HORA" not in str(col_data).upper():
        df["data"] = df["data"].astype(str) + " " + df_fonte[col_hora].astype(str)

    df["data"] = df["data"].astype(str).str.strip('"')
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce")
    df = df.dropna(subset=["valor"])
    if df.empty:
        return []

    return df.sort_values("data").tail(limite_pontos).to_dict("records")


def calcular_estatisticas(serie, unidade=""):
    if not serie:
        return None
    valores = [p["valor"] for p in serie]
    media = sum(valores) / len(valores)

    if len(valores) >= 6:
        terco = max(1, len(valores) // 3)
        diferenca = sum(valores[-terco:]) / terco - sum(valores[:terco]) / terco
        limiar = max(0.5, abs(media) * 0.03)
        if diferenca > limiar:
            tendencia = f"📈 Subindo (+{diferenca:.1f}{unidade})"
        elif diferenca < -limiar:
            tendencia = f"📉 Descendo ({diferenca:.1f}{unidade})"
        else:
            tendencia = "➡️ Estável"
    else:
        tendencia = "— (poucos pontos)"

    return {
        "media": round(media, 1),
        "minima": round(min(valores), 1),
        "maxima": round(max(valores), 1),
        "tendencia": tendencia,
        "n_pontos": len(valores),
    }

# ==========================================================
# ESTADO E TABELA UNIFICADA
# ==========================================================

tabela = pd.DataFrame()
tabela_lock = threading.Lock()
ultima_atualizacao = None
_ultimo_bloco_inmet = pd.DataFrame()


def montar_tabela():
    global _ultimo_bloco_inmet

    df_hora = coletar_om_horario()
    df_prev = coletar_om_diario()
    df_estacao = coletar_estacao_meteorologica()
    df_comunidade = coletar_dados_comunidade()

    df_inmet_novo = coletar_inmet()
    if not df_inmet_novo.empty:
        _ultimo_bloco_inmet = df_inmet_novo
        _marcar_diagnostico("INMET (fonte usada)", True, len(df_inmet_novo), "API de tempo real")
    else:
        _marcar_diagnostico("INMET (fonte usada)", False, 0, "API sem dados; mantendo último bloco")

    partes = [d for d in [df_hora, df_prev, df_estacao, df_comunidade, _ultimo_bloco_inmet] if not d.empty]
    if not partes:
        return pd.DataFrame()

    montada = pd.concat(partes, ignore_index=True)
    cols = ["Estacao"] + [c for c in montada.columns if c != "Estacao"]
    return montada[cols]

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
        return

    dados_str = dados.fillna("—").astype(str)
    linhas = [dados_str.columns.tolist()] + dados_str.values.tolist()

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
            log.warning(f"Erro do Sheets na aba '{nome_aba}' (tentativa {tentativa}); aguardando {espera}s... ({e})")
            time.sleep(espera)
        except Exception as e:
            log.error(f"Erro ao escrever aba '{nome_aba}': {e}")
            return
    log.error(f"Falha ao escrever aba '{nome_aba}' após {tentativas} tentativas.")

# ── Aba "Gráficos" ─────────────────────────────────────────

ABA_GRAFICOS = "Gráficos"
_GRAF_LINHAS_MAX = 200


def _dados_graficos(df):
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
    import gspread

    linhas_hora, linhas_dia = _dados_graficos(df)
    if not linhas_hora and not linhas_dia:
        return

    try:
        try:
            ws = sh.worksheet(ABA_GRAFICOS)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=ABA_GRAFICOS, rows=_GRAF_LINHAS_MAX, cols=20)

        ws.batch_clear([f"A1:F{_GRAF_LINHAS_MAX}"])

        if linhas_hora:
            ws.update(values=[["Hora", "Temperatura (°C)"]] + linhas_hora,
                      range_name="A1", value_input_option="USER_ENTERED")
        if linhas_dia:
            ws.update(values=[["Data", "Temp. Máx (°C)", "Temp. Mín (°C)"]] + linhas_dia,
                      range_name="D1", value_input_option="USER_ENTERED")

        metadados = sh.fetch_sheet_metadata(
            params={"fields": "sheets(properties(sheetId,title),charts(chartId))"}
        )
        ja_tem_grafico = any(
            aba.get("properties", {}).get("title") == ABA_GRAFICOS and aba.get("charts")
            for aba in metadados.get("sheets", [])
        )
        total = len(linhas_hora) + len(linhas_dia)
        if ja_tem_grafico:
            _marcar_diagnostico("Gráficos (aba)", True, total, "dados reescritos")
            return

        sh.batch_update({"requests": [
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
        ]})
        _marcar_diagnostico("Gráficos (aba)", True, total, "gráficos criados")

    except Exception as e:
        log.error(f"Erro ao montar a aba de gráficos: {e}")
        _marcar_diagnostico("Gráficos (aba)", False, 0, str(e))


def exportar_para_sheets(df):
    try:
        import gspread  # noqa: F401
    except ImportError:
        log.warning("gspread/google-auth não instalados. Verifique o requirements.txt.")
        return

    if not os.path.exists(GSHEETS_CREDENCIAIS):
        log.warning(f"Credenciais não encontradas em {GSHEETS_CREDENCIAIS}.")
        return

    try:
        gc = _obter_cliente_gspread()
        sh = gc.open(GSHEETS_NOME)

        _escrever_aba_com_retry(sh, "Diagnóstico", montar_aba_diagnostico(), pular_dedup=True)

        if df.empty:
            log.warning("Tabela principal vazia; só a aba Diagnóstico foi atualizada.")
            return

        _escrever_aba_com_retry(sh, "Todas", df)
        _atualizar_aba_graficos(sh, df)

        for fonte in df["Estacao"].unique():
            df_f = df[df["Estacao"] == fonte]
            cols = [c for c in df.columns if df_f[c].notna().any()]
            _escrever_aba_com_retry(sh, fonte[:100], df_f[cols])

        log.info(f"Google Sheets sincronizado: '{GSHEETS_NOME}'")
    except Exception as e:
        log.error(f"Erro ao exportar para Google Sheets: {e}")

# ==========================================================
# ATUALIZAÇÃO E AGENDADOR
# ==========================================================

_ciclo_lock = threading.Lock()


def atualizar_dados():
    global tabela, ultima_atualizacao
    # evita dois ciclos ao mesmo tempo (ex.: envio de CSV durante o agendado)
    if not _ciclo_lock.acquire(blocking=False):
        log.info("Já existe um ciclo em andamento; ignorando este.")
        return
    try:
        log.info("Iniciando ciclo de atualização...")
        nova = montar_tabela()
        with tabela_lock:
            if not nova.empty:
                tabela = nova
                ultima_atualizacao = datetime.now()
                log.info(f"Atualização concluída: {len(tabela)} registros.")
            else:
                log.warning("Sem dados novos; mantendo a tabela anterior.")

        if GSHEETS_ATIVO:
            with tabela_lock:
                df_copia = tabela.copy()
            exportar_para_sheets(df_copia)
    finally:
        _ciclo_lock.release()


def proxima_execucao():
    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    return base + timedelta(hours=INTERVALO_ATUALIZACAO_HORAS)


def agendador():
    while True:
        alvo = proxima_execucao()
        segundos = (alvo - datetime.now()).total_seconds()
        log.info(f"Próxima atualização: {alvo.strftime('%d/%m/%Y às %H:%M')}")
        time.sleep(max(segundos, 1))
        try:
            atualizar_dados()
        except Exception as e:
            log.error(f"Erro inesperado no ciclo: {e}")

# ==========================================================
# FLASK
# ==========================================================

app = Flask(__name__)


def gerar_linhas(df):
    linhas = []
    for _, row in df.iterrows():
        css = FONTE_CSS.get(row.get("Estacao", ""), "")
        celulas = "".join(f"<td>{html.escape(str(v))}</td>" for v in row.values)
        linhas.append(f'<tr class="{css}">{celulas}</tr>')
    return "\n".join(linhas)


@app.route("/")
def inicio():
    with tabela_lock:
        df_atual = tabela.copy()

    if df_atual.empty:
        return "<h1>Buscando os dados pela primeira vez... atualize a página em alguns segundos.</h1>"

    fontes = list(df_atual["Estacao"].unique())
    fonte_ativa = flask_request.args.get("fonte", "todas")
    df_exibir = df_atual if fonte_ativa == "todas" else df_atual[df_atual["Estacao"] == fonte_ativa]

    if fonte_ativa == "todas":
        colunas_mostrar = list(df_atual.columns)
    else:
        colunas_mostrar = [c for c in df_atual.columns if df_exibir[c].notna().any()]
        if "Estacao" not in colunas_mostrar:
            colunas_mostrar = ["Estacao"] + colunas_mostrar

    cabecalho_html = "".join(f"<th>{html.escape(str(c))}</th>" for c in colunas_mostrar)
    linhas_html = gerar_linhas(df_exibir[colunas_mostrar].fillna("—").head(500))

    ativo_todas = "btn-ativo" if fonte_ativa == "todas" else ""
    botoes = f'<a href="/" class="btn btn-todas {ativo_todas}">🌐 Todas</a>\n'
    for fonte in fontes:
        css = FONTE_CSS.get(fonte, "")
        ativo = "btn-ativo" if fonte == fonte_ativa else ""
        ico = ICONES.get(fonte, "")
        url = f"/?fonte={requests.utils.quote(fonte)}"
        botoes += f'<a href="{url}" class="btn {css} {ativo}">{ico} {html.escape(fonte)}</a>\n'

    total = len(df_atual)
    exibido = min(500, len(df_exibir))
    rodape = ultima_atualizacao.strftime("%d/%m/%Y às %H:%M:%S") if ultima_atualizacao else "—"

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
h1 {{ color: #1565C0; margin-bottom: 4px; }}
p {{ margin: 4px 0 14px; color: #555; font-size: 13px; }}
.banner-gs {{ background: #E8F5E9; border: 1px solid #A5D6A7; border-radius: 8px; padding: 10px 16px; margin-bottom: 14px; font-size: 13px; color: #1B5E20; }}
.banner-gs a {{ color: #1B5E20; }}
.banner-add {{ background: #EDE7F6; border: 1px solid #B39DDB; border-radius: 8px; padding: 10px 16px; margin-bottom: 14px; font-size: 13px; color: #4527A0; }}
.banner-add a {{ color: #4527A0; }}
.filtros {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }}
.btn {{ padding: 7px 16px; border-radius: 20px; font-size: 13px; font-weight: bold; text-decoration: none; border: 2px solid transparent; cursor: pointer; }}
.btn:hover {{ opacity: .8; }}
.btn-ativo {{ border-color: #222 !important; }}
.btn-todas {{ background: #e0e0e0; color: #333; }}
.src-om-hora {{ background: #B3E5FC; color: #01579B; }}
.src-om-prev {{ background: #C5CAE9; color: #1A237E; }}
.src-estacao-meteo {{ background: #FFE0B2; color: #E65100; }}
.src-cachoeira {{ background: #C8E6C9; color: #1B5E20; }}
.src-sm-auto {{ background: #FFF9C4; color: #E65100; }}
.src-sm-conv {{ background: #F8BBD0; color: #880E4F; }}
.src-comunidade {{ background: #D1C4E9; color: #4527A0; }}
.wrapper {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; background: white; min-width: 900px; }}
thead th {{ background: #1565C0; color: white; padding: 8px 6px; position: sticky; top: 0; white-space: nowrap; }}
td {{ border: 1px solid #ddd; padding: 5px 6px; text-align: center; white-space: nowrap; }}
tr.src-om-hora td {{ background: #E1F5FE; }}
tr.src-om-prev td {{ background: #E8EAF6; }}
tr.src-estacao-meteo td {{ background: #FFF3E0; }}
tr.src-cachoeira td {{ background: #E8F5E9; }}
tr.src-sm-auto td {{ background: #FFFDE7; }}
tr.src-sm-conv td {{ background: #FCE4EC; }}
tr.src-comunidade td {{ background: #EDE7F6; }}
</style>
</head>
<body>
<h1>🌦️ Clima Unificado – INMET + Open-Meteo</h1>
<p>Total de registros: <strong>{total}</strong> &nbsp;|&nbsp;
Exibindo: <strong>{exibido}</strong> &nbsp;|&nbsp;
Fonte: <strong>{html.escape(fonte_ativa)}</strong> &nbsp;|&nbsp;
Última atualização: <strong>{rodape}</strong>
&nbsp;|&nbsp; <a href="/graficos" style="color:#1565C0; font-weight:bold;">📊 Gráficos e tendências →</a></p>
{banner_sheets}
{banner_comunidade}
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


@app.route("/adicionar", methods=["GET", "POST"])
def adicionar_dado():
    erro = None
    sucesso_qtd = None
    ignoradas = 0

    if flask_request.method == "POST":
        nome = flask_request.form.get("nome", "").strip()
        arquivo = flask_request.files.get("arquivo")

        if not arquivo or arquivo.filename == "":
            erro = "Selecione um arquivo .csv para enviar."
        elif not arquivo.filename.lower().endswith(".csv"):
            erro = "O arquivo precisa ser um .csv."
        else:
            try:
                conteudo = arquivo.read(2_000_000)  # limite de ~2 MB
                linhas, total, ignoradas = _processar_csv_comunidade(conteudo, nome)
                if not linhas:
                    erro = "Nenhuma linha com dados foi encontrada nesse CSV."
                else:
                    _salvar_dados_comunidade(linhas)
                    sucesso_qtd = len(linhas)
                    threading.Thread(target=atualizar_dados, daemon=True).start()
            except Exception as e:
                log.error(f"Erro ao processar CSV da comunidade: {e}")
                erro = "Não foi possível ler esse arquivo. Confira o formato (CSV) e tente novamente."

    mensagem_html = ""
    if sucesso_qtd is not None:
        extra = f" ({ignoradas} linha(s) ignorada(s) por estarem vazias)" if ignoradas else ""
        mensagem_html = (f'<div class="msg msg-ok">✅ {sucesso_qtd} registro(s) enviado(s){extra}! '
                         f'Pode levar alguns segundos para aparecer na tabela.</div>')
    elif erro:
        mensagem_html = f'<div class="msg msg-erro">⚠️ {html.escape(erro)}</div>'

    colunas_modelo = "".join(f"<li>{c}</li>" for c in CAMPOS_COMUNIDADE)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Adicionar dados — Clima Unificado</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 20px; background: #f0f2f5; }}
h1 {{ color: #4527A0; margin-bottom: 4px; }}
p.info {{ color: #555; font-size: 13px; margin-bottom: 20px; }}
a.voltar {{ font-size: 13px; color: #1565C0; text-decoration: none; }}
form {{ background: white; border-radius: 10px; padding: 20px 24px; max-width: 460px; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
label {{ display: block; font-size: 13px; font-weight: bold; color: #333; margin: 12px 0 4px; }}
input[type="text"], input[type="file"] {{ width: 100%; padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; box-sizing: border-box; background: white; }}
button {{ margin-top: 18px; background: #4527A0; color: white; border: none; padding: 10px 22px; border-radius: 20px; font-size: 14px; font-weight: bold; cursor: pointer; }}
button:hover {{ opacity: .85; }}
.msg {{ padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 16px; max-width: 460px; }}
.msg-ok {{ background: #E8F5E9; border: 1px solid #A5D6A7; color: #1B5E20; }}
.msg-erro {{ background: #FFEBEE; border: 1px solid #EF9A9A; color: #B71C1C; }}
.modelo {{ background: #F3E5F5; border: 1px solid #CE93D8; border-radius: 8px; padding: 12px 16px; max-width: 460px; font-size: 12px; color: #4A148C; margin-top: 18px; }}
.modelo ul {{ margin: 6px 0 8px 18px; padding: 0; }}
.modelo a {{ color: #4527A0; font-weight: bold; }}
</style>
</head>
<body>
<a class="voltar" href="/">← Voltar para a tabela</a>
<h1>🙋 Adicionar dados meteorológicos</h1>
<p class="info">Envie um arquivo CSV com seus dados (de uma planilha, estação caseira, etc.).
Qualquer CSV é aceito, com as colunas que você já tiver. Cada linha vira um registro
identificado como "Dados da Comunidade".</p>
{mensagem_html}
<form method="POST" action="/adicionar" enctype="multipart/form-data">
<label>Nome ou apelido (usado se o CSV não tiver essa coluna)</label>
<input type="text" name="nome" placeholder="Opcional">
<label>Arquivo CSV</label>
<input type="file" name="arquivo" accept=".csv" required>
<button type="submit">Enviar CSV</button>
</form>
<div class="modelo">
Nenhuma coluna é obrigatória. Colunas parecidas com estas são reconhecidas automaticamente
(sem acento, "temp", "chuva" etc. também funcionam):
<ul>{colunas_modelo}</ul>
Qualquer outra coluna é mantida como veio.
<br><a href="/modelo-comunidade.csv">📥 Baixar modelo de CSV (opcional)</a>
</div>
</body>
</html>"""


@app.route("/modelo-comunidade.csv")
def modelo_comunidade_csv():
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


@app.route("/graficos")
def graficos():
    with tabela_lock:
        df_atual = tabela.copy()

    if df_atual.empty:
        return ("<h1>Ainda sem dados pra mostrar gráficos. "
                "<a href='/'>Volte pra página principal</a> e aguarde a primeira coleta.</h1>")

    cores = ["#1565C0", "#F2A65A", "#6FCF97", "#E88888", "#9575CD", "#4FD1E8", "#4527A0"]
    fontes_disponiveis = list(df_atual["Estacao"].unique())

    blocos_html = []
    for metrica, info in METRICAS_HISTORICAS.items():
        datasets_js = []
        linhas_stats = []

        for i, fonte in enumerate(fontes_disponiveis):
            df_fonte = df_atual[df_atual["Estacao"] == fonte]
            serie = extrair_serie_metrica(df_fonte, metrica)
            if not serie:
                continue

            cor = cores[i % len(cores)]
            pontos_js = ", ".join(f'{{x: "{p["data"]}", y: {p["valor"]}}}' for p in serie)
            datasets_js.append(
                f'{{label: "{fonte}", data: [{pontos_js}], borderColor: "{cor}", '
                f'backgroundColor: "{cor}", tension: 0.25, pointRadius: 2, borderWidth: 2}}'
            )

            est = calcular_estatisticas(serie, info["unidade"])
            if est:
                u = info["unidade"]
                linhas_stats.append(
                    f"<tr><td>{html.escape(fonte)}</td>"
                    f"<td>{est['media']} {u}</td><td>{est['minima']} {u}</td>"
                    f"<td>{est['maxima']} {u}</td><td>{est['tendencia']}</td>"
                    f"<td>{est['n_pontos']}</td></tr>"
                )

        if not datasets_js:
            continue

        canvas_id = f"grafico_{metrica}"
        datasets_str = ", ".join(datasets_js)
        titulo_y = f"{info['titulo']} ({info['unidade']})"

        blocos_html.append(f"""
<section class="bloco">
<h2>{info['titulo']} ({info['unidade']})</h2>
<canvas id="{canvas_id}" height="110"></canvas>
<table class="stats">
<thead><tr><th>Fonte</th><th>Média</th><th>Mínima</th><th>Máxima</th><th>Tendência</th><th>Pontos</th></tr></thead>
<tbody>{''.join(linhas_stats)}</tbody>
</table>
<script>
new Chart(document.getElementById("{canvas_id}"), {{
  type: "line",
  data: {{ datasets: [{datasets_str}] }},
  options: {{ scales: {{ x: {{ type: "category" }}, y: {{ title: {{ display: true, text: "{titulo_y}" }} }} }} }}
}});
</script>
</section>""")

    corpo = "".join(blocos_html) or "<p>Nenhuma fonte tem dados reconhecíveis para gráficos no momento.</p>"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Gráficos — Clima Unificado</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
body {{ font-family: Arial, sans-serif; margin: 20px; background: #f0f2f5; }}
h1 {{ color: #1565C0; }}
a.voltar {{ font-size: 13px; color: #1565C0; text-decoration: none; }}
.bloco {{ background: white; border-radius: 10px; padding: 16px 20px; margin: 18px 0; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
table.stats {{ border-collapse: collapse; width: 100%; font-size: 12px; margin-top: 12px; }}
table.stats th {{ background: #1565C0; color: white; padding: 6px; }}
table.stats td {{ border: 1px solid #ddd; padding: 5px 6px; text-align: center; }}
</style>
</head>
<body>
<a class="voltar" href="/">← Voltar para a tabela</a>
<h1>📊 Gráficos e tendências</h1>
{corpo}
</body>
</html>"""


def _data_mais_recente_por_fonte(df):
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
            datas = df_f[col_data].astype(str)
            col_hora = encontrar_coluna(df_f.columns, "HORA") or encontrar_coluna(df_f.columns, "HR", "MEDICAO")
            if col_hora is not None:
                datas = datas + " " + df_f[col_hora].astype(str)
            resultado[fonte] = datas.max() if len(datas) else "sem dados"
        except Exception as e:
            resultado[fonte] = f"erro ao calcular: {e}"
    return resultado


@app.route("/status")
def status():
    with tabela_lock:
        df_atual = tabela.copy()
    datas = _data_mais_recente_por_fonte(df_atual)
    linhas = "".join(f"<li><strong>{html.escape(f)}</strong>: {html.escape(str(d))}</li>"
                     for f, d in datas.items())
    ultima = ultima_atualizacao.strftime("%d/%m/%Y %H:%M:%S") if ultima_atualizacao else "—"
    return (f"<h1>Status</h1><p>Última atualização: {ultima}</p>"
            f"<ul>{linhas or '<li>Sem dados ainda</li>'}</ul>"
            f"<a href='/'>← Voltar</a>")

# ==========================================================
# INICIALIZAÇÃO
# ==========================================================


def _iniciar_em_segundo_plano():
    try:
        atualizar_dados()
    except Exception as e:
        log.error(f"Erro na primeira coleta: {e}")
    agendador()


threading.Thread(target=_iniciar_em_segundo_plano, daemon=True).start()

if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=porta)
