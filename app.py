pontos_js = ", ".join(f'{{x: "{p["data"]}", y: {p["valor"]}}}' for p in serie)
            datasets_js.append(
                f'{{label: "{fonte}", data: [{pontos_js}], borderColor: "{cor}", '
                f'backgroundColor: "{cor}", tension: 0.25, pointRadius: 2, borderWidth: 2}}'
            )

            est = calcular_estatisticas(serie, info["unidade"])
            if est:
                linhas_stats.append(
                    f"<tr><td>{fonte}</td>"
                    f"<td>{est['media']} {info['unidade']}</td>"
                    f"<td>{est['minima']} {info['unidade']}</td>"
                    f"<td>{est['maxima']} {info['unidade']}</td>"
                    f"<td>{est['tendencia']}</td>"
                    f"<td>{est['n_pontos']}</td></tr>"
                )

        if not datasets_js:
            continue

        canvas_id    = f"grafico_{metrica}"
        datasets_str = ", ".join(datasets_js)
        titulo_y     = f"{info['titulo']} ({info['unidade']})"

        blocos_html.append(f"""
        <section class="bloco">
            <h2>{info['titulo']} ({info['unidade']})</h2>
            <canvas id="{canvas_id}" height="110"></canvas>
            <table class="stats">
                <thead><tr><th>Fonte</th><th>Média</th><th>Mínima</th><th>Máxima</th>
                <th>Tendência</th><th>Pontos</th></tr></thead>
                <tbody>{''.join(linhas_stats)}</tbody>
            </table>
            <script>
                new Chart(document.getElementById("{canvas_id}"), {{
                    type: "line",
                    data: {{ datasets: [{datasets_str}] }},
                    options: {{
                        scales: {{
                            x: {{ type: "category" }},
                            y: {{ title: {{ display: true, text: "{titulo_y}" }} }}
                        }}
                    }}
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
        .bloco {{ background: white; border-radius: 10px; padding: 16px 20px;
                  margin: 18px 0; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
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
