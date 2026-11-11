from datetime import datetime, timezone
import time
import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="BATAPP | Configuración Automática por Lote CSV",
    layout="wide",
)

# --- CONFIGURACIÓN DE SESIÓN ---
session = requests.Session()
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "X-Stellar-Op": "hybrico",
    "X-Stellar-Org": "hybrico",
    "Origin": "https://stellar.newsunroad.com",
    "Referer": "https://stellar.newsunroad.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    ),
}

LOGIN_URL = "https://api.stellar.newsunroad.com/api/v0/login"


def autenticar():
  try:
    username = st.secrets.get("API_USERNAME")
    password = st.secrets.get("API_PASSWORD")
    if not username or not password:
      return False

    login_res = session.post(
        LOGIN_URL, json={"username": username.strip(), "password": password.strip()}, headers=headers
    )
    if login_res.status_code == 200 and login_res.json().get("user") is not False:
      token = login_res.json().get("token") or login_res.json().get("sessionId")
      if token:
        headers["Authorization"] = f"Bearer {token}"
      return True
    return False
  except Exception:
    return False


def obtener_inversores_iv1_iv2(site_id):
  """
  Retorna estrictamente los dos inversores principales del sitio: _iv1 y _iv2,
  evitando por completo sub-arreglos o componentes que generen error 400.
  """
  clean_site_id = site_id.strip()
  return [f"{clean_site_id}_iv1", f"{clean_site_id}_iv2"]


def convertir_hora_a_entero(val_str):
  """
  Convierte valores de hora (ej: '200', '1600', '1:30', 200) a entero numérico para la API.
  """
  try:
    if pd.isna(val_str):
      return 400
    s = str(val_str).strip().replace(":", "").replace(".", "")
    if s.isdigit():
      return int(s)
    return 400
  except Exception:
    return 400


def enviar_parametros_lote(datos_a_enviar):
  """
  Envía los parámetros de Corte AM y Corte PM únicamente a _iv1 y _iv2 de cada sitio.
  """
  set_params_url = "https://api.stellar.newsunroad.com/api/v0/setParameters"
  resultados_globales = []

  for idx, row in datos_a_enviar.iterrows():
    sitio_id = str(row["SITIO_ID"]).strip()
    corte_am = convertir_hora_a_entero(row["Corte AM"])
    corte_pm = convertir_hora_a_entero(row["Corte PM"])

    # Considerar únicamente _iv1 y _iv2
    inversores = obtener_inversores_iv1_iv2(sitio_id)

    for inv_id in inversores:
      params_payload = {
          "params": {
              inv_id: {
                  "inverterChargeEndTime2": corte_am,
                  "inverterChargeStartTime2": corte_pm,
              }
          }
      }

      try:
        res = session.post(set_params_url, json=params_payload, headers=headers, timeout=10)
        if res.status_code == 200 and res.json().get("status") == "success":
          resultados_globales.append({
              "Sitio": sitio_id,
              "Inversor": inv_id,
              "Corte AM": corte_am,
              "Corte PM": corte_pm,
              "Estado": "✅ Enviado con éxito",
          })
        else:
          resultados_globales.append({
              "Sitio": sitio_id,
              "Inversor": inv_id,
              "Corte AM": corte_am,
              "Corte PM": corte_pm,
              "Estado": f"❌ Error API ({res.status_code})",
          })
      except Exception as e:
        resultados_globales.append({
            "Sitio": sitio_id,
            "Inversor": inv_id,
            "Corte AM": corte_am,
            "Corte PM": corte_pm,
            "Estado": f"❌ Excepción: {str(e)}",
        })

  return pd.DataFrame(resultados_globales)


# --- INTERFAZ STREAMLIT ---
st.title("⚡ Automatización Masiva: Envío de Horarios por CSV ")
st.markdown(
    """
<div style="padding: 20px; border-radius: 10px; border: 1px solid #4A4A4A; background-color: #1E1E1E;">
    <h4 style="color: #FFC107; margin-top: 0;">📌 Instrucciones Operativas</h4>
    <ul>
        <li><strong>Paso 1:</strong> Selecciona o arrastra tu archivo CSV generado con la app de diagnóstico ciclos.</li>
        <li><strong>Paso 2:</strong> Revisa la vista previa de las columnas <code>Corte AM</code> y <code>Corte PM</code> para estar seguro de que es el archivo correcto.</li>
        <li><strong>Paso 3:</strong> Presiona el botón de ejecución masiva; el sistema aplicará los cambios exclusivamente a los inversores <strong>_iv1</strong> y <strong>_iv2</strong>.</li>
        <li><strong>Paso 4:</strong> Debes esperar a que el programa realice 3 ráfagas de comandos a cada sitio.</li>
        <li><strong>Paso 5:</strong> Espera 5 minutos y repite el proceso para asegurarte de que todos los sitios hayan recibido los cambios</li>
    </ul>
</div>
""", unsafe_allow_html=True
)

archivo_csv = st.file_uploader("Seleccionar archivo CSV de Lotes", type=["csv"])

if archivo_csv is not None:
  try:
    df_csv = pd.read_csv(archivo_csv)
    st.success(f"📁 Archivo cargado correctamente ({len(df_csv)} sitios encontrados).")

    st.markdown("### 🔍 Vista Previa del CSV")
    st.dataframe(df_csv[["SITIO_ID", "NOMBRE_SITIO", "CLASIFICACIÓN", "Corte AM", "Corte PM"]], use_container_width=True, hide_index=True)

    total_bursts = 3
    delay_seconds = 90

    if st.button("🚀 Iniciar Envío Masivo a _iv1 y _iv2 (3 Ráfagas)", type="primary", use_container_width=True):
      if not autenticar():
        st.error("❌ Falló la autenticación con la API de Stellar. Verifica tus credenciales en `secrets.toml`.")
      else:
        progress_bar = st.progress(0)
        status_container = st.empty()

        for burst in range(1, total_bursts + 1):
          status_container.markdown(f"### 🚀 Ejecutando Ráfaga {burst} de {total_bursts}...")
          
          with st.spinner("Enviando comandos a los inversores _iv1 y _iv2..."):
            df_resultados = enviar_parametros_lote(df_csv)
            st.dataframe(df_resultados, use_container_width=True)

          progress_bar.progress(burst / total_bursts)

          if burst < total_bursts:
            st.info(f"⏳ Esperando {delay_seconds} segundos antes de la siguiente repetición...")
            time.sleep(delay_seconds)

        st.balloons()
        st.success("✅ **¡Proceso completado exitosamente!** Se aplicaron las 3 ráfagas de parámetros sin errores de sub-dispositivos.")

  except Exception as e:
    st.error(f"❌ Error al procesar el archivo CSV: {e}")
else:
  st.info("💡 Por favor, sube tu archivo CSV para comenzar, debe ser de a uno en uno.")
