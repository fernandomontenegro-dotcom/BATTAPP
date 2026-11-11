from datetime import date, datetime, timedelta, timezone
import io
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(
    page_title="BATAPP | Diagnóstico Operativo & Lotes",
    layout="wide",
)

# --- CONFIGURACIÓN DE SESIÓN ---
session = requests.Session()
session.headers.update({
    "X-STELLAR-OP": "hybrico",
    "X-STELLAR-ORG": "hybrico",
    "Origin": "https://stellar.newsunroad.com",
    "Referer": "https://stellar.newsunroad.com/",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    ),
})

LOGIN_URL = "https://api.stellar.newsunroad.com/api/v0/login"


def autenticar():
    try:
        payload = {
            "username": st.secrets["API_USERNAME"],
            "password": st.secrets["API_PASSWORD"],
        }
        res = session.post(LOGIN_URL, json=payload, timeout=10)
        return res.status_code == 200
    except Exception:
        return False


def redondear_a_media_hora_inferior(dt):
    if not dt:
        return None
    minutos_totales = dt.hour * 60 + dt.minute
    minutos_redondeados = (minutos_totales // 30) * 30
    horas = (minutos_redondeados // 60) % 24
    minutos = minutos_redondeados % 60
    return dt.replace(hour=horas, minute=minutos, second=0, microsecond=0)


def redondear_a_media_hora(dt):
    if not dt:
        return None
    minutos_totales = dt.hour * 60 + dt.minute
    minutos_redondeados = round(minutos_totales / 30) * 30
    horas = (minutos_redondeados // 60) % 24
    minutos = minutos_redondeados % 60
    return dt.replace(hour=horas, minute=minutos, second=0, microsecond=0)


def parse_time(t_val):
    if t_val is None:
        return None
    if isinstance(t_val, (int, float)):
        return datetime.fromtimestamp(
            t_val / 1000 if t_val > 1e11 else t_val, tz=timezone.utc
        )
    try:
        dt = datetime.fromisoformat(str(t_val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def obtener_multiplicador(data_dict, parametro):
    try:
        unidad = (
            data_dict.get("meta", {})
            .get("parameterUnits", {})
            .get(parametro, "")
            .lower()
        )
        return 1000 if unidad == "kw" else 1
    except Exception:
        return 1


def extraer_datos_y_tiempo(data_dict, claves_buscadas):
    if isinstance(claves_buscadas, str):
        claves_buscadas = [claves_buscadas]
    items = data_dict.get("data", [])
    suma_total = 0.0
    dato_encontrado = False
    tiempos_dt = []

    if items and isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            inner_data = item.get("data", {})
            for clave in claves_buscadas:
                if clave in inner_data:
                    val = inner_data[clave]
                    num = None
                    if isinstance(val, dict):
                        num = val.get("last", val.get("mean"))
                        dt = parse_time(val.get("time"))
                        if dt:
                            tiempos_dt.append(dt)
                    else:
                        num = val

                    if num is not None:
                        try:
                            suma_total += float(num)
                            dato_encontrado = True
                        except Exception:
                            pass

    tiempo_reciente = max(tiempos_dt) if tiempos_dt else None
    return (suma_total if dato_encontrado else None), tiempo_reciente


def evaluar_estado_sitio(site_id, start_sec, end_sec, ahora_utc):
    parametros = (
        "inverterLoadPower,acLoadPower,batteryBankVoltage,batteryModuleVoltage,batteryModuleStateOfCharge,batteryVoltage,"
        "solarChargerPower,inverterArrayPower"
    )
    url = f"https://api.stellar.newsunroad.com/api/v0/devices/{site_id}?end={end_sec}&start={start_sec}&params={parametros}"

    try:
        res = session.get(url, timeout=10)
        if res.status_code != 200:
            return "Error API"
        data_dict = res.json()

        raw_inv, t_inv = extraer_datos_y_tiempo(data_dict, 'inverterLoadPower')
        mult_inv = obtener_multiplicador(data_dict, 'inverterLoadPower')
        load_power = (raw_inv * mult_inv) if raw_inv is not None else None

        raw_med, t_med = extraer_datos_y_tiempo(data_dict, 'acLoadPower')
        mult_med = obtener_multiplicador(data_dict, 'acLoadPower')
        met_power = (raw_med * mult_med) if raw_med is not None else None

        _, t_bat = extraer_datos_y_tiempo(data_dict, ['batteryBankVoltage', 'batteryModuleVoltage', 'batteryModuleStateOfCharge'])

        datos_viejos = False
        tiempo_evaluar = t_inv or t_med or t_bat
        if tiempo_evaluar:
            if (ahora_utc - tiempo_evaluar).total_seconds() / 60 > 45:
                datos_viejos = True

        if (load_power is None and met_power is None) or datos_viejos:
            return "Offline / Sin Gestión"
        elif load_power is not None:
            medidor_inactivo = (met_power is None or met_power <= 10)
            if load_power < 500 and medidor_inactivo:
                return "Bypass"
            elif met_power is None:
                return "Normal (Sin Medidor)"
            else:
                return "Normal"
        else:
            return "Atípico"
    except Exception:
        return "Error Excepción"


def extraer_datos_banco_exacto(site_id, start_dt, end_dt):
    start_sec = int(start_dt.timestamp())
    end_sec = int(end_dt.timestamp())

    params_str = (
        "batteryBankVoltage,batteryBankStateOfCharge,batteryBankCycles,"
        "batteryBankStateOfHealth,batteryBankCapacity,batteryBankEnergyCapacity,"
        "batteryBankCurrent,batteryBankPower,batteryBankSynchronizations,batteryBankTemperature"
    )

    url = f"https://api.stellar.newsunroad.com/api/v0/devices/{site_id}?end={end_sec}&start={start_sec}&params={params_str}&deviceClass=BatteryBankMonitor"

    ah_capacitancia = 0.0
    voltaje_banco = 0.0

    try:
        res = session.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            items = data.get("data", [])

            for item in items:
                fuentes_a_revisar = []
                if isinstance(item, dict):
                    if "data" in item and isinstance(item["data"], dict):
                        fuentes_a_revisar.append(item["data"])
                    if "timeSeries" in item and isinstance(item["timeSeries"], list):
                        if item["timeSeries"]:
                            fuentes_a_revisar.append(item["timeSeries"][-1])
                    fuentes_a_revisar.append(item)

                for fuente in fuentes_a_revisar:
                    for k in ["batteryBankCapacity", "capacity", "batteryBankEnergyCapacity"]:
                        if k in fuente and ah_capacitancia == 0.0:
                            val_obj = fuente[k]
                            if isinstance(val_obj, dict):
                                v_num = val_obj.get("last") or val_obj.get("mean") or val_obj.get("value")
                            else:
                                v_num = val_obj
                            if v_num is not None:
                                try:
                                    val_f = float(v_num)
                                    if val_f > 0:
                                        ah_capacitancia = val_f
                                except:
                                    pass

                    for k in ["batteryBankVoltage", "voltage"]:
                        if k in fuente and voltaje_banco == 0.0:
                            val_obj = fuente[k]
                            if isinstance(val_obj, dict):
                                v_num = val_obj.get("last") or val_obj.get("mean") or val_obj.get("value")
                            else:
                                v_num = val_obj
                            if v_num is not None:
                                try:
                                    val_f = float(v_num)
                                    if 40.0 <= val_f <= 70.0:
                                        voltaje_banco = val_f
                                except:
                                    pass
    except Exception:
        pass

    return ah_capacitancia, voltaje_banco


def consultar_series_temporales(site_id, start_dt, end_dt):
    start_sec = int(start_dt.timestamp())
    end_sec = int(end_dt.timestamp())

    # --- SE AÑADE gridVoltageLLMean A LA CONSULTA ---
    params_str = "solarPower,batteryStateOfCharge,inverterLoadPower,gridPower,gridVoltageLLMean"
    url = f"https://api.stellar.newsunroad.com/api/v0/ts/{site_id}?end={end_sec}&start={start_sec}&params={params_str}"

    try:
        res = session.get(url, timeout=10)
        if res.status_code == 200:
            return res.json(), url
    except Exception:
        pass

    return None, url


def procesar_respuesta_ts(data_json):
    if not data_json or "data" not in data_json:
        return pd.DataFrame()

    registros = []
    items = data_json.get("data", [])

    for item in items:
        if isinstance(item, dict):
            time_series = item.get("timeSeries", [])
            for ts in time_series:
                if isinstance(ts, dict):
                    raw_ts = ts.get("time") or ts.get("timestamp") or ts.get("t")

                    fila = {"Timestamp": raw_ts}
                    hora_local_str = str(raw_ts)
                    dt_local_obj = None

                    if raw_ts is not None:
                        try:
                            if isinstance(raw_ts, (int, float)) or (isinstance(raw_ts, str) and raw_ts.isdigit()):
                                val_sec = int(raw_ts)
                                if val_sec > 1000000000000:
                                    val_sec = val_sec // 1000
                                dt_utc = datetime.fromtimestamp(val_sec, tz=timezone.utc)
                            elif isinstance(raw_ts, str):
                                dt_utc = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                            else:
                                dt_utc = None

                            if dt_utc:
                                dt_local_obj = dt_utc.astimezone(timezone(timedelta(hours=-6)))
                                hora_local_str = dt_local_obj.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass

                    fila["Hora Local (CST)"] = hora_local_str
                    fila["dt_obj"] = dt_local_obj

                    # --- SE AÑADE gridVoltageLLMean AL PARSEO ---
                    for param in ["solarPower", "batteryStateOfCharge", "inverterLoadPower", "gridPower", "gridVoltageLLMean"]:
                        val = ts.get(param)
                        try:
                            fila[param] = float(val) if val is not None else 0.0
                        except:
                            fila[param] = 0.0

                    registros.append(fila)

    if not registros:
        return pd.DataFrame()

    df = pd.DataFrame(registros)
    if "Hora Local (CST)" in df.columns:
        df = df.sort_values("Hora Local (CST)").reset_index(drop=True)
    return df


def calcular_metricas_dia(site_id, fecha_estudio):
    tz_cst = timezone(timedelta(hours=-6))
    dt_inicio_cst = datetime(
        fecha_estudio.year, fecha_estudio.month, fecha_estudio.day,
        0, 0, 0, tzinfo=tz_cst
    )
    dt_fin_cst = dt_inicio_cst + timedelta(days=1)

    ah_banco, voltaje_banco = extraer_datos_banco_exacto(site_id, dt_inicio_cst, dt_fin_cst)
    raw_data, _ = consultar_series_temporales(site_id, dt_inicio_cst, dt_fin_cst)
    df_telemetria = procesar_respuesta_ts(raw_data)

    # --- NUEVO FILTRO: PRESENCIA DE RED (gridVoltageLLMean) >= 75% ---
    if not df_telemetria.empty and "gridVoltageLLMean" in df_telemetria.columns:
        df_sorted = df_telemetria.dropna(subset=["dt_obj"]).sort_values("dt_obj").reset_index(drop=True)
        if len(df_sorted) > 1:
            dt_h = df_sorted["dt_obj"].diff().dt.total_seconds() / 3600.0
            dt_h = dt_h.fillna(0)
            
            # Se asume presencia de red si el voltaje L-L es mayor a 50V
            tiempo_con_red = dt_h[df_sorted["gridVoltageLLMean"] > 50.0].sum()
            
            # 75% de 24 horas = 18 horas
            if tiempo_con_red < 18.0:
                return {
                    "df": df_telemetria,
                    "FALLA_RED": True,
                    "Tiempo Red (h)": round(tiempo_con_red, 2)
                }

    # ---------- CARGA PROMEDIO ----------
    carga_promedio_kw = 0.0
    if not df_telemetria.empty and "inverterLoadPower" in df_telemetria.columns:
        cargas_validas = df_telemetria[df_telemetria["inverterLoadPower"] > 0]["inverterLoadPower"]
        carga_promedio_kw = (
            cargas_validas.mean() if not cargas_validas.empty
            else df_telemetria["inverterLoadPower"].mean()
        )
    carga_promedio_kw_ajustada = carga_promedio_kw * 0.75
    carga_promedio_90 = carga_promedio_kw * 0.90

    # ---------- TIEMPO DE RESPALDO Y FALLA BAT ----------
    falla_bat = False
    if ah_banco <= 0 or voltaje_banco <= 0:
        falla_bat = True
        tiempo_respaldo_horas = 0.0
    else:
        energia_nominal_wh = ah_banco * voltaje_banco
        carga_promedio_watts = carga_promedio_kw_ajustada * 1000.0 if carga_promedio_kw_ajustada > 0 else 1.0
        tiempo_respaldo_horas = energia_nominal_wh / carga_promedio_watts if carga_promedio_watts > 0 else 0.0

    # ---------- INFLECCIONES ----------
    punto_inicio = None
    punto_fin = None

    if not df_telemetria.empty:
        df_telemetria["diff"] = df_telemetria["solarPower"] - df_telemetria["inverterLoadPower"]
        df_telemetria["prev_diff"] = df_telemetria["diff"].shift(1)

        cruce_subida = df_telemetria[(df_telemetria["diff"] > 0) & (df_telemetria["prev_diff"] <= 0)]
        if not cruce_subida.empty:
            punto_inicio = cruce_subida.iloc[0]

        cruce_bajada = df_telemetria[(df_telemetria["diff"] < 0) & (df_telemetria["prev_diff"] >= 0)]
        if not cruce_bajada.empty:
            punto_fin = cruce_bajada.iloc[-1]

    # ---------- ENERGÍA: PRODUCCIÓN vs. EXCEDENTE (FACTOR 0.90) ----------
    produccion_solar_kwh = 0.0
    excedente_solar_kwh = 0.0

    if not df_telemetria.empty and len(df_telemetria) > 1:
        df_sorted = df_telemetria.dropna(subset=["dt_obj"]).sort_values("dt_obj").reset_index(drop=True)
        if len(df_sorted) > 1:
            dt_h = df_sorted["dt_obj"].diff().dt.total_seconds() / 3600.0
            dt_h = dt_h.fillna(0)

            solar = df_sorted["solarPower"].astype(float)
            load = df_sorted["inverterLoadPower"].astype(float)

            min_sl = pd.concat([solar, load], axis=1).min(axis=1)
            max_ex = (solar - load).clip(lower=0)

            prod_integral = ((min_sl + min_sl.shift(1).fillna(min_sl)) / 2.0) * dt_h
            exce_integral = ((max_ex + max_ex.shift(1).fillna(max_ex)) / 2.0) * dt_h

            produccion_solar_kwh = max(0.0, float(prod_integral.sum()))
            excedente_solar_kwh = max(0.0, float(exce_integral.sum()))

    autonomia_excedente_horas = excedente_solar_kwh / carga_promedio_90 if carga_promedio_90 > 0 else 0.0

    # ---------- PRODUCCIÓN SOLAR HORAS (PUNTO 1) ----------
    produccion_solar_horas = 0.0
    if punto_inicio is not None and punto_fin is not None:
        dt_i = punto_inicio["dt_obj"]
        dt_f = punto_fin["dt_obj"]
        if dt_i and dt_f:
            df_slice = df_telemetria[(df_telemetria["dt_obj"] >= dt_i) & (df_telemetria["dt_obj"] <= dt_f)].copy()
            if len(df_slice) > 1:
                df_slice = df_slice.sort_values("dt_obj").reset_index(drop=True)
                dt_h_slice = df_slice["dt_obj"].diff().dt.total_seconds() / 3600.0
                dt_h_slice = dt_h_slice.fillna(0)
                is_valid = df_slice["solarPower"] >= (0.95 * df_slice["inverterLoadPower"])
                produccion_solar_horas = float(dt_h_slice[is_valid].sum())

    autonomia_acumulada_horas = ( produccion_solar_horas + autonomia_excedente_horas)

    # ---------- CORTE PM ----------
    corte_pm_str = "N/A"
    if punto_fin is not None:
        soc_en_cruce = punto_fin["batteryStateOfCharge"]
        dt_cruce_pm = punto_fin["dt_obj"]
        if dt_cruce_pm:
            dt_corte_pm = dt_cruce_pm
            if soc_en_cruce < 90:
                dt_corte_pm = dt_corte_pm - timedelta(minutes=30)
            elif 90 <= soc_en_cruce <= 95:
                dt_corte_pm = dt_corte_pm
            else:
                dt_corte_pm = dt_corte_pm + timedelta(minutes=30)

            dt_corte_pm_redondeado = redondear_a_media_hora(dt_corte_pm)
            corte_pm_str = dt_corte_pm_redondeado.strftime("%H%M")

    # ---------- CORTE AM ----------
    corte_am_str = "N/A"
    if punto_inicio is not None:
        dt_cruce_am = punto_inicio["dt_obj"]
        if dt_cruce_am:
            dt_corte_am = dt_cruce_am - timedelta(hours=autonomia_excedente_horas)
            dt_corte_am_inferior = redondear_a_media_hora_inferior(dt_corte_am)

            soc_ajuste = None
            if punto_fin is not None:
                soc_ajuste = punto_fin["batteryStateOfCharge"]
            elif not df_telemetria.empty:
                soc_ajuste = df_telemetria["batteryStateOfCharge"].max()

            if soc_ajuste is not None:
                if soc_ajuste < 80:
                    dt_corte_am_inferior += timedelta(hours=1)
                elif soc_ajuste < 90:
                    dt_corte_am_inferior += timedelta(minutes=30)
                elif soc_ajuste <= 97:
                    pass
                else:
                    dt_corte_am_inferior -= timedelta(minutes=30)

            corte_am_str = dt_corte_am_inferior.strftime("%H%M")

            if not df_telemetria.empty and corte_am_str not in ("N/A", "0000"):
                df_full = df_telemetria[df_telemetria["batteryStateOfCharge"] >= 99.5]
                if not df_full.empty:
                    first_full_dt = df_full.iloc[0]["dt_obj"]
                    if first_full_dt is not None and first_full_dt.hour < 13:
                        try:
                            dt_ca = datetime.strptime(corte_am_str, "%H%M")
                            dt_ca -= timedelta(hours=1)
                            corte_am_str = dt_ca.strftime("%H%M")
                        except Exception:
                            pass

    # DoD
    dod_potencial_str = "0%"
    if falla_bat:
        dod_potencial_str = "(Falla Bat)"
    elif tiempo_respaldo_horas > 0:
        dod_val = 100 - (autonomia_excedente_horas / tiempo_respaldo_horas) * 100.0
        dod_potencial_str = f"{round(dod_val, 1)}%"

    return {
        "FALLA_RED": False,
        "Tiempo Red (h)": 24.0, # Si pasó el filtro, no importa el valor exacto
        "Ah": round(ah_banco, 1) if not falla_bat else "(Falla Bat)",
        "Voltage": round(voltaje_banco, 1) if not falla_bat else "(Falla Bat)",
        "Carga Promedio (kW)": round(carga_promedio_kw, 3),
        "Carga Prom 0.90 (kW)": round(carga_promedio_90, 3),
        "Horas de Autonomía": round(tiempo_respaldo_horas, 2) if not falla_bat else "(Falla Bat)",
        "Excedente Solar (kWh)": round(excedente_solar_kwh, 2),
        "Producción Solar (kWh)": round(produccion_solar_kwh, 2),
        "Producción Solar (Horas)": round(produccion_solar_horas, 2),
        "Autonomía Excedente (Horas)": round(autonomia_excedente_horas, 2),
        "Autonomía Acumulada (Horas)": round(autonomia_acumulada_horas, 2),
        "DoD Potencial": dod_potencial_str,
        "Corte AM": corte_am_str,
        "Corte PM": corte_pm_str,
        "df": df_telemetria,
        "punto_inicio": punto_inicio,
        "punto_fin": punto_fin,
        "carga_promedio_kw": carga_promedio_kw,
    }


def clasificar_sitio(res):
    df_t = res["df"]
    corte_am = res.get("Corte AM", "N/A")
    corte_pm = res.get("Corte PM", "N/A")

    if df_t.empty:
        return "Sin datos", "Serie temporal vacía", corte_am, corte_pm

    solar = df_t["solarPower"]
    load = df_t["inverterLoadPower"]
    soc = df_t["batteryStateOfCharge"]

    if not (solar > load).any():
        return ("Conexión Permanente",
                "Sin excedente solar (solar nunca > carga)",
                "0000", "0000")

    max_soc = float(soc.max()) if not soc.empty else 0.0

    df_full = df_t[df_t["batteryStateOfCharge"] >= 99.5]
    if not df_full.empty:
        first_full_dt = df_full.iloc[0]["dt_obj"]
        if first_full_dt is not None and first_full_dt.hour < 13:
            return ("Cuestionable o Saturado",
                    f"SOC 100% antes de 13:00 ({first_full_dt.strftime('%H:%M')})",
                    corte_am, corte_pm)

    if max_soc < 85.0:
        return ("Cuestionable o Saturado",
                f"SOC máximo {max_soc:.1f}% (<85%)",
                corte_am, corte_pm)

    df_s = df_t.dropna(subset=["batteryStateOfCharge", "dt_obj"]).sort_values("dt_obj")
    if len(df_s) > 1:
        dt_h = df_s["dt_obj"].diff().dt.total_seconds() / 3600.0
        d_soc = df_s["batteryStateOfCharge"].diff()
        valid = dt_h > 0
        if valid.any():
            rates = (d_soc[valid] / dt_h[valid])
            if not rates.empty and rates.min() < -25.0:
                return ("Cuestionable o Saturado",
                        f"Descarga acelerada ({rates.min():.1f}%/h)",
                        corte_am, corte_pm)

    excedente = res.get("Excedente Solar (kWh)", 0.0)
    if excedente >= 0.5 and max_soc >= 85.0:
        return "Ciclo Regular", "", corte_am, corte_pm
    return "Ciclo Regular", "Mala producción (excedente muy bajo)", corte_am, corte_pm


# --- INTERFAZ STREAMLIT PRINCIPAL ---
st.title("⚡ Diagnóstico Operativo (Individual & Lotes por Región)")
st.markdown("---")

tab_ind, tab_lote = st.tabs(["📊 Sitio Individual", "📁 Consulta por Lote (Regiones GT & HN)"])

# ==========================================
# PESTAÑA 1: MODO INDIVIDUAL
# ==========================================
with tab_ind:
    site_id_ind = st.text_input("ID del Sitio (Individual):", value="hyb_aguacatan_-_hht780")
    st.info("📅 **Nota:** El análisis se calcula usando los datos del **día de ayer**. Se requiere ≥75% del día con presencia de Red.")

    if st.button("🚀 Ejecutar Diagnóstico Individual", type="primary", use_container_width=True):
        if not autenticar():
            st.error("❌ Falló la autenticación con la API de Stellar.")
        else:
            with st.spinner("Verificando estado operativo y presencia de red..."):
                ahora_utc = datetime.now(timezone.utc)
                hace_horas = ahora_utc - timedelta(hours=4)
                estado_ind = evaluar_estado_sitio(
                    site_id_ind.strip(),
                    int(hace_horas.timestamp()),
                    int(ahora_utc.timestamp()),
                    ahora_utc,
                )

            if estado_ind not in ("Normal", "Normal (Sin Medidor)"):
                st.error(
                    f"❌ El sitio **{site_id_ind}** está en estado **'{estado_ind}'**. "
                    "Solo se analizan sitios en estado **Normal** o **Normal (Sin Medidor)**."
                )
            else:
                fecha_ayer = date.today() - timedelta(days=1)
                with st.spinner("Consultando datos y calculando parámetros del día previo..."):
                    res_ind = calcular_metricas_dia(site_id_ind.strip(), fecha_ayer)

                # --- VALIDACIÓN DE RED ---
                if res_ind.get("FALLA_RED", False):
                    tiempo_red = res_ind.get("Tiempo Red (h)", 0.0)
                    st.error(
                        f"❌ **Filtro de Red Reprobado:** El sitio solo tuvo presencia de red "
                        f"(gridVoltageLLMean > 50V) durante **{tiempo_red} horas** el día de ayer. "
                        "Se requiere al menos el 75% (18 horas) para considerarse operativo en red."
                    )
                elif res_ind["df"].empty:
                    st.warning("⚠️ No se encontraron registros para el día de ayer en este sitio.")
                else:
                    st.success(f"✅ Estado: **{estado_ind}** | Red: **Suficiente**. Extrayendo datos del {fecha_ayer.strftime('%d/%m/%Y')}...")
                    
                    st.markdown(f"### 🔋 Parámetros Extraídos y Autonomía Nominal (Día: {fecha_ayer.strftime('%d/%m/%Y')})")
                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric(label="Capacidad Extraída", value=f"{res_ind['Ah']} Ah", delta="batteryBankCapacity")
                    m2.metric(label="Voltaje Extraído", value=f"{res_ind['Voltage']} V", delta="batteryBankVoltage")
                    m3.metric(label="Inverter Load Promedio", value=f"{res_ind['Carga Promedio (kW)']} kW", delta="Promedio 24h")
                    m4.metric(label="Tiempo de Respaldo (75%)", value=f"{res_ind['Horas de Autonomía']} Horas", delta="Autonomía Batería")
                    m5.metric(label="Excedente Solar", value=f"{res_ind['Excedente Solar (kWh)']} kWh", delta="∫ max(solar−carga,0)")

                    st.markdown("### ☀️ Aprovechamiento Energético")
                    a1, a2, a3, a4, a5 = st.columns(5)
                    a1.metric(
                        label="Producción Solar (h efectivas)",
                        value=f"{res_ind['Producción Solar (Horas)']} Horas",
                        delta="Tiempo >=95% de carga entre inflexiones"
                    )
                    a2.metric(label="Autonomía Excedente", value=f"{res_ind['Autonomía Excedente (Horas)']} Horas", delta="Excedente / (carga × 0.90)")
                    a3.metric(label="Tiempo de Respaldo", value=f"{res_ind['Horas de Autonomía']} Horas", delta="Autonomía Batería")
                    a4.metric(label="Autonomía Acumulada", value=f"{res_ind['Autonomía Acumulada (Horas)']} Horas", delta=" Producción + Excedente")
                    a5.metric(label="DoD% Potencial", value=res_ind["DoD Potencial"], delta="Descarga objetivo")

                    st.markdown("### ⏰ Horarios de Corte Operativo")
                    c_col1, c_col2 = st.columns(2)
                    c_col1.metric(label="CORTE AM", value=res_ind["Corte AM"], delta="Ajustado por SOC")
                    c_col2.metric(label="CORTE PM", value=res_ind["Corte PM"], delta="Ajuste por rangos de SOC")

                    df_t = res_ind["df"]
                    st.markdown("---")
                    st.markdown("### 📊 Gráfica Comparativa: Solar vs Carga (Con Puntos de Inflexión)")

                    fig_cruce = go.Figure()
                    fig_cruce.add_trace(go.Scatter(x=df_t["Hora Local (CST)"], y=df_t["solarPower"], name="Solar Power (kW)", line=dict(color="#FFC107", width=2.5)))
                    fig_cruce.add_trace(go.Scatter(x=df_t["Hora Local (CST)"], y=df_t["inverterLoadPower"], name="Inverter Load Power (kW)", line=dict(color="#FF5722", width=2.5)))

                    if res_ind["punto_inicio"] is not None:
                        p_ini = res_ind["punto_inicio"]
                        fig_cruce.add_trace(go.Scatter(
                            x=[p_ini["Hora Local (CST)"]], y=[p_ini["solarPower"]],
                            mode="markers+text", name="Cruce Ascendente",
                            text=[f"🌅 Inicio: {p_ini['Hora Local (CST)'][-8:]}"], textposition="top center",
                            marker=dict(color="#00E676", size=14, symbol="triangle-up")
                        ))

                    if res_ind["punto_fin"] is not None:
                        p_fin = res_ind["punto_fin"]
                        fig_cruce.add_trace(go.Scatter(
                            x=[p_fin["Hora Local (CST)"]], y=[p_fin["solarPower"]],
                            mode="markers+text", name="Cruce Descendente",
                            text=[f"🌇 Fin: {p_fin['Hora Local (CST)'][-8:]} (SOC: {p_fin['batteryStateOfCharge']}%)"], textposition="top center",
                            marker=dict(color="#FF1744", size=14, symbol="triangle-down")
                        ))

                    fig_cruce.update_layout(title="<b>Puntos de Inflexión: Generación Solar vs Carga del Inversor</b>", xaxis_title="Hora Local (CST)", yaxis_title="Potencia (kW)", template="plotly_dark", height=450, hovermode="x unified")
                    st.plotly_chart(fig_cruce, use_container_width=True)

                    st.markdown("---")
                    st.markdown("### 📊 Gráficas Operativas Adicionales")
                    for param, titulo, color in [("gridPower", "Potencia de la Red (Grid Power)", "#E040FB"), ("batteryStateOfCharge", "Estado de Carga de la Batería (SOC)", "#00B0FF")]:
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=df_t["Hora Local (CST)"], y=df_t[param], name=param, line=dict(color=color, width=3), fill="tozeroy"))
                        fig.update_layout(title=f"<b>{titulo}</b>", xaxis_title="Hora Local (CST)", yaxis_title="Valor", template="plotly_dark", height=350, hovermode="x unified")
                        st.plotly_chart(fig, use_container_width=True)

                    st.markdown("---")
                    st.markdown("### 📋 Tabla de Muestreo (Granularidad de 15 minutos)")
                    st.dataframe(df_t.drop(columns=["diff", "prev_diff", "dt_obj"], errors="ignore"), use_container_width=True)


# ==========================================
# PESTAÑA 2: MODO LOTES
# ==========================================
with tab_lote:
    st.markdown("### 📁 Consulta por Lote (Carga Masiva por Región HN y GT)")
    st.markdown(
        "Carga automáticamente todos los sitios de la organización seleccionada, "
        "filtra **solo** los sitios en estado **Normal** o **Normal (Sin Medidor)** con **presencia de red >75%** "
        "y los clasifica automáticamente analizando el comportamiento del **día de ayer**."
    )

    region_lote = st.selectbox("Selecciona la Región/Organización:", ["Tigo GT", "Honduras"])
    st.info("📅 **Nota:** Los cálculos usarán el día de ayer. Los sitios con Falla de Red (<18h) serán omitidos de las tablas.")

    if st.button("🚀 Cargar Topología y Procesar Lote por Región", type="primary", use_container_width=True):
        if not autenticar():
            st.error("❌ Falló la autenticación con la API de Stellar.")
        else:
            with st.spinner("Descargando organizaciones y mapeando sitios de la red..."):
                res_orgs = session.get("https://api.stellar.newsunroad.com/api/v0/organizations", timeout=15)
                org_list = res_orgs.json().get('data', []) if res_orgs.status_code == 200 else []

                sitios_candidatos = []
                for org in org_list:
                    nombre_org = org.get('name', '')
                    if (region_lote.lower() in nombre_org.lower()
                            or (region_lote == "Tigo GT" and "tigo gt" in nombre_org.lower())
                            or (region_lote == "Honduras" and "honduras" in nombre_org.lower())):
                        for view in org.get('powerSystemViews', []):
                            if isinstance(view, dict) and view.get('viewOf'):
                                sitios_candidatos.append({
                                    "REGION": nombre_org,
                                    "NOMBRE_SITIO": str(view.get('viewOf')).replace('_', ' ').replace('-', ' ').upper(),
                                    "SITIO_ID": view.get('viewOf')
                                })

            if not sitios_candidatos:
                st.warning(f"⚠️ No se encontraron sitios para la región seleccionada: {region_lote}")
            else:
                st.info(f"🔍 Topología cargada: {len(sitios_candidatos)} sitios. Filtrando estado, red y clasificando...")

                ahora_utc = datetime.now(timezone.utc)
                hace_horas = ahora_utc - timedelta(hours=4)
                end_sec = int(ahora_utc.timestamp())
                start_sec = int(hace_horas.timestamp())
                fecha_ayer = date.today() - timedelta(days=1)

                resultados_lote = []
                barra_progreso = st.progress(0)
                total_sitios = len(sitios_candidatos)

                for i, sitio in enumerate(sitios_candidatos):
                    s_id = sitio["SITIO_ID"]
                    nombre_s = sitio["NOMBRE_SITIO"]

                    estado_sitio = evaluar_estado_sitio(s_id, start_sec, end_sec, ahora_utc)

                    if estado_sitio in ("Normal", "Normal (Sin Medidor)"):
                        try:
                            res = calcular_metricas_dia(s_id, fecha_ayer)
                            
                            # Si falla el filtro de red, se ignora por completo
                            if res.get("FALLA_RED", False):
                                pass 
                            elif not res["df"].empty:
                                clasif, anomalia, corte_am, corte_pm = clasificar_sitio(res)

                                resultados_lote.append({
                                    "SITIO_ID": s_id,
                                    "NOMBRE_SITIO": nombre_s,
                                    "ESTADO": estado_sitio,
                                    "CLASIFICACIÓN": clasif,
                                    "ANOMALÍA": anomalia,
                                    "Producción Solar (h)": res["Producción Solar (Horas)"],
                                    "Autonomía Excedente (h)": res["Autonomía Excedente (Horas)"],
                                    "Carga Prom 0.90 (kW)": res.get("Carga Prom 0.90 (kW)", 0.0),
                                    "DoD Potencial": res["DoD Potencial"],
                                    "Excedente Solar (kWh)": res["Excedente Solar (kWh)"],
                                    "Corte AM": corte_am,
                                    "Corte PM": corte_pm,
                                })
                        except Exception:
                            pass

                    barra_progreso.progress((i + 1) / total_sitios)

                if resultados_lote:
                    df_lote_final = pd.DataFrame(resultados_lote)
                    st.success(
                        f"✅ Procesamiento completado. {len(df_lote_final)} sitios en estado Normal y con Red válida "
                        "fueron clasificados usando el día de ayer."
                    )
                    st.session_state["df_lote_final"] = df_lote_final
                    st.session_state["region_lote"] = region_lote
                else:
                    st.warning("⚠️ No se encontraron sitios válidos (Normal + Red >75%) para esta región.")
                    if "df_lote_final" in st.session_state:
                        del st.session_state["df_lote_final"]

    # Renderizar resultados si existen
    if "df_lote_final" in st.session_state and not st.session_state["df_lote_final"].empty:
        df_lote_final = st.session_state["df_lote_final"]
        region_lote = st.session_state["region_lote"]
        ayer_str = (date.today() - timedelta(days=1)).strftime('%Y%m%d')

        def get_anomaly_priority(anomaly):
            anomaly = str(anomaly)
            if "SOC 100%" in anomaly: return 1
            if "SOC máximo" in anomaly: return 2
            if "Descarga acelerada" in anomaly: return 3
            if "Mala producción" in anomaly: return 4
            return 5

        df_perm = df_lote_final[df_lote_final["CLASIFICACIÓN"] == "Conexión Permanente"].copy()
        
        df_cuest = df_lote_final[df_lote_final["CLASIFICACIÓN"] == "Cuestionable o Saturado"].copy()
        if not df_cuest.empty:
            df_cuest["_sort_anomaly"] = df_cuest["ANOMALÍA"].apply(get_anomaly_priority)
            df_cuest = df_cuest.sort_values(by=["_sort_anomaly", "SITIO_ID"]).drop(columns=["_sort_anomaly"])

        df_reg = df_lote_final[df_lote_final["CLASIFICACIÓN"] == "Ciclo Regular"].copy()
        if not df_reg.empty:
            df_reg["_sort_anomaly"] = df_reg["ANOMALÍA"].apply(get_anomaly_priority)
            df_reg = df_reg.sort_values(by=["_sort_anomaly", "SITIO_ID"]).drop(columns=["_sort_anomaly"])

        st.markdown("### 📊 Resumen General de Lotes (Día de Ayer + Filtro Red)")
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("🔴 Conexión Permanente", len(df_perm))
        col_m2.metric("🟡 Cuestionable o Saturado", len(df_cuest))
        col_m3.metric("🟢 Ciclo Regular", len(df_reg))

        # ---- Conexión Permanente ----
        st.markdown("---")
        st.markdown(f"## 🔴 Conexión Permanente ({len(df_perm)} sitios)")
        if not df_perm.empty:
            st.dataframe(df_perm, use_container_width=True, hide_index=True)
            csv_perm = df_perm.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Descargar CSV: Conexión Permanente",
                data=csv_perm,
                file_name=f"lote_{region_lote.lower()}_conexion_permanente_{ayer_str}.csv",
                mime="text/csv",
                use_container_width=True,
                key="dl_perm",
            )
        else:
            st.info("No hay sitios en esta categoría.")

        # ---- Cuestionable o Saturado ----
        st.markdown("---")
        st.markdown(f"## 🟡 Cuestionable o Saturado ({len(df_cuest)} sitios)")
        if not df_cuest.empty:
            st.dataframe(df_cuest, use_container_width=True, hide_index=True)
            csv_cuest = df_cuest.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Descargar CSV: Cuestionable o Saturado",
                data=csv_cuest,
                file_name=f"lote_{region_lote.lower()}_cuestionable_{ayer_str}.csv",
                mime="text/csv",
                use_container_width=True,
                key="dl_cuest",
            )
        else:
            st.info("No hay sitios en esta categoría.")

        # ---- Ciclo Regular ----
        st.markdown("---")
        st.markdown(f"## 🟢 Ciclo Regular ({len(df_reg)} sitios)")
        if not df_reg.empty:
            st.dataframe(df_reg, use_container_width=True, hide_index=True)
            csv_reg = df_reg.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Descargar CSV: Ciclo Regular",
                data=csv_reg,
                file_name=f"lote_{region_lote.lower()}_ciclo_regular_{ayer_str}.csv",
                mime="text/csv",
                use_container_width=True,
                key="dl_reg",
            )
        else:
            st.info("No hay sitios en esta categoría.")
