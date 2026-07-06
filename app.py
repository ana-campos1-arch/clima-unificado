# ==========================================================
# CLIMA UNIFICADO - INMET + OPEN-METEO
# Versão para deploy no Render + Google Sheets
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
# ==========================================================

import pandas as pd
import requests
import zipfile
import io
import threading
import time
import os
from flask import Flask, request as flask_request
from datetime import datetime, date, timedelta

# ==========================================================
# ⚙️  CONFIGURAÇÕES PRINCIPAIS
# ==========================================================

LATITUDE  = -30.0397
LONGITUDE = -52.8930

ESTACOES = {
    "B822":  "Cachoeira do Sul",
    "A803":  "Santa Maria Automática",
    "83936": "Santa Maria Convencional"
}

URL_2026 = "https://portal.inmet.gov.br/uploads/dadoshistoricos/2026.zip"

# De quantas em quantas horas os dados são atualizados automaticamente.
INTERVALO_ATUALIZACAO_HORAS = 1

# ── Google Sheets ────────────────────────────────────────
GSHEETS_ATIVO       = True
GSHEETS_CREDENCIAIS = os.environ.get("GSHEETS_CREDENCIAIS_PATH", "/etc/secrets/credenciais.json")
GSHEETS_NOME        = os.environ.get("GSHEETS_NOME", "Clima Unificado")
GSHEETS_LINK        = os.environ.get(
    "GSHEETS_LINK",
    "https://docs.google.com/spreadsheets/d/1yDFMkt0-Buuijc1LwVc6Sj8Zixk74cc0izi0azvT5sA/edit?gid=192990081#gid=192990081"
)

# ==========================================================
# FONTE → CLASSE CSS
# ==========================================================

FONTE_CSS = {
    "Open-Meteo – Hoje (horário)":       "src-om-hora",
    "Open-Meteo – Previsão (diária)":    "src-om-prev",
    "INMET - Cachoeira do Sul":          "src-cachoeira",
    "INMET - Santa Maria Automática":    "src-sm-auto",
    "INMET - Santa Maria Convencional":  "src-sm-conv",
}

# ==========================================================
# OPEN-METEO: HOJE HORA A HORA
# ==========================================================

def coletar_om_horario():
    print("Buscando Open-Meteo – hoje hora a hora...")
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
        dados = requests.get(url, timeout=30).json()
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
        print(f"OK: {len(linhas)} horas do dia de hoje")
        return pd.DataFrame(linhas)
    except Exception as e:
        print(f"Erro Open-Meteo horário: {e}")
        return pd.DataFrame()

# ==========================================================
# OPEN-METEO: PREVISÃO DIÁRIA (próximos 16 dias)
# ==========================================================

def coletar_om_diario():
    print("Buscando Open-Meteo – previsão diária...")
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
        dados = requests.get(url, timeout=30).json()
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
        print(f"OK: {len(linhas)} dias de previsão")
        return pd.DataFrame(linhas)
    except Exception as e:
        print(f"Erro Open-Meteo diário: {e}")
        return pd.DataFrame()

# ==========================================================
# INMET
# ==========================================================

def coletar_inmet():
    print("Baixando dados do INMET...")
    try:
        resposta = requests.get(URL_2026, timeout=120)
        zip_file = zipfile.ZipFile(io.BytesIO(resposta.content))
        arquivos = zip_file.namelist()
        dados = []
        for codigo, cidade in ESTACOES.items():
            for arq in arquivos:
                if codigo in arq:
                    try:
                        df = pd.read_csv(
                            zip_file.open(arq),
                            sep=";", encoding="latin1",
                            skiprows=8, low_memory=False
                        )
                        df.insert(0, "Estacao", f"INMET - {cidade}")
                        dados.append(df)
                        print(f"OK: INMET - {cidade}")
                    except Exception as e:
                        print(f"Erro {cidade}: {e}")
        if not dados:
            return pd.DataFrame()
        return pd.concat(dados, ignore_index=True)
    except Exception as e:
        print(f"Erro ao baixar INMET: {e}")
        return pd.DataFrame()

def encontrar_coluna(colunas, *chaves):
    for col in colunas:
        nome = str(col).upper()
        if all(chave.upper() in nome for chave in chaves):
            return col
    return None

# ==========================================================
# MONTAR TABELA UNIFICADA
# ==========================================================

def montar_tabela():
    df_hora  = coletar_om_horario()
    df_prev  = coletar_om_diario()
    df_inmet = coletar_inmet()
    partes = [df for df in [df_hora, df_prev, df_inmet] if not df.empty]
    if not partes:
        return pd.DataFrame()
    tabela = pd.concat(partes, ignore_index=True)
    cols = ["Estacao"] + [c for c in tabela.columns if c != "Estacao"]
    return tabela[cols]

# ==========================================================
# ESTADO COMPARTILHADO
# ==========================================================

tabela             = pd.DataFrame()
tabela_lock        = threading.Lock()
ultima_atualizacao = None

# ==========================================================
# GOOGLE SHEETS
# ==========================================================

def exportar_para_sheets(df):
    if df.empty:
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("AVISO: gspread/google-auth não instalados. Verifique o requirements.txt.")
        return

    if not os.path.exists(GSHEETS_CREDENCIAIS):
        print(f"AVISO: credenciais não encontradas em {GSHEETS_CREDENCIAIS}. "
              f"Configure o Secret File no Render.")
        return

    try:
        escopos = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(GSHEETS_CREDENCIAIS, scopes=escopos)
        gc    = gspread.authorize(creds)
        sh    = gc.open(GSHEETS_NOME)

        def escrever_aba(nome_aba, dados):
            dados_str = dados.fillna("—").astype(str)
            linhas    = [dados_str.columns.tolist()] + dados_str.values.tolist()
            try:
                ws = sh.worksheet(nome_aba)
                ws.clear()
            except gspread.exceptions.WorksheetNotFound:
                ws = sh.add_worksheet(title=nome_aba, rows=len(linhas) + 10, cols=len(dados_str.columns))
            ws.update(linhas)

        escrever_aba("Todas", df)

        for fonte in df["Estacao"].unique():
            df_f = df[df["Estacao"] == fonte]
            cols = [c for c in df.columns if df_f[c].notna().any()]
            escrever_aba(fonte[:100], df_f[cols])

        print(f"✅ Google Sheets atualizado: '{GSHEETS_NOME}'")
    except Exception as e:
        print(f"Erro ao exportar para Google Sheets: {e}")

# ==========================================================
# ATUALIZAÇÃO DE DADOS
# ==========================================================

def atualizar_dados():
    global tabela, ultima_atualizacao
    print(f"\n[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] Iniciando atualização dos dados...")
    nova_tabela = montar_tabela()
    with tabela_lock:
        if not nova_tabela.empty:
            tabela = nova_tabela
            ultima_atualizacao = datetime.now()
            print(f"Atualização concluída: {len(tabela)} registros.")
        else:
            print("A atualização não trouxe dados novos; mantendo a tabela anterior.")

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
        alvo    = proxima_execucao()
        segundos = (alvo - datetime.now()).total_seconds()
        print(f"Próxima atualização automática agendada para {alvo.strftime('%d/%m/%Y às %H:%M')}")
        time.sleep(max(segundos, 1))
        atualizar_dados()

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

        .src-om-hora   {{ background: #B3E5FC; color: #01579B; }}
        .src-om-prev   {{ background: #C5CAE9; color: #1A237E; }}
        .src-cachoeira {{ background: #C8E6C9; color: #1B5E20; }}
        .src-sm-auto   {{ background: #FFF9C4; color: #E65100; }}
        .src-sm-conv   {{ background: #F8BBD0; color: #880E4F; }}

        .wrapper {{ overflow-x: auto; }}
        table    {{ border-collapse: collapse; width: 100%; font-size: 12px; background: white; min-width: 900px; }}
        thead th {{
            background: #1565C0; color: white; padding: 8px 6px;
            position: sticky; top: 0; white-space: nowrap;
        }}
        td {{ border: 1px solid #ddd; padding: 5px 6px; text-align: center; white-space: nowrap; }}

        tr.src-om-hora   td {{ background: #E1F5FE; }}
        tr.src-om-prev   td {{ background: #E8EAF6; }}
        tr.src-cachoeira td {{ background: #E8F5E9; }}
        tr.src-sm-auto   td {{ background: #FFFDE7; }}
        tr.src-sm-conv   td {{ background: #FCE4EC; }}
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

@app.route("/status")
def status():
    """Endpoint simples para checar se o servidor está vivo (útil para keep-alive)."""
    return {"ok": True, "ultima_atualizacao": str(ultima_atualizacao)}

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
    print("Buscando dados iniciais...")
    atualizar_dados()

    # Agendador automático (roda em segundo plano, dentro do mesmo processo)
    threading.Thread(target=agendador, daemon=True).start()

    porta = int(os.environ.get("PORT", 5000))
    print(f"Servidor iniciado na porta {porta}")
    if GSHEETS_ATIVO:
        print(f"\n{'='*60}")
        print(f"📊 PLANILHA GOOGLE SHEETS:")
        print(f"   {GSHEETS_LINK}")
        print(f"{'='*60}\n")
    app.run(host="0.0.0.0", port=porta, threaded=True)
