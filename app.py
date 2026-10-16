import io
import re
import unicodedata
from datetime import date, datetime

import fitz
import pandas as pd
import streamlit as st

st.set_page_config(page_title='SICOSEP | CENASE', page_icon='🛡️', layout='wide')
st.title('🛡️ CENASE | Control de capacitaciones SICOSEP')
st.caption('Carga certificados PDF, revisa los datos extraídos y descarga un Excel resumido con control independiente de Nivel 1 y Nivel 2.')

PERIODO = 24

def normalize(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(s)) if not unicodedata.combining(c)).upper()

def iso_date(s):
    s = str(s).strip()
    if not s:
        return ''
    try:
        return pd.to_datetime(s, errors='raise').date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return ''

def extract_field(text, label, next_labels=()):
    """Extract a value displayed on the line after a label in certificates."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    wanted = normalize(label).rstrip(':')
    for i, line in enumerate(lines):
        if normalize(line).rstrip(':') == wanted and i + 1 < len(lines):
            return lines[i + 1]
    return ''

def parse_course(section):
    date_match = re.search(r'Fecha de Registro\s*\n?\s*(\d{4}-\d{2}-\d{2})', section, flags=re.I)
    convocatoria = re.search(r'Número de Convocatoria\s*:\s*\n?\s*([^\n]+)', section, flags=re.I)
    return (date_match.group(1) if date_match else '', convocatoria.group(1).strip() if convocatoria else '')

DATE_TOKEN = r"(?<!\d)(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]20\d{2})(?!\d)"

def dates_in(text):
    result = []
    for m in re.finditer(DATE_TOKEN, text):
        parsed = iso_date(m.group(0))
        if parsed and parsed not in result:
            result.append(parsed)
    return result

def parse_retrain(section):
    """Extrae fechas de la subsección Reentrenamientos del NIVEL correcto.

    Acepta fechas en renglón separado, filas de tabla y formatos AAAA-MM-DD,
    DD/MM/AAAA. Nunca toma la fecha del curso ni inventa reentrenamientos.
    """
    match = re.search(r"Re\s*entrenamientos?\s*:?", normalize(section), re.I)
    if not match:
        return []
    # normalize preserva la longitud de los encabezados ASCII habituales
    sub = section[match.end():]
    sub = re.split(r"(?:^|\n)\s*(?:IMPORTANTE|OBSERVACIONES|ACREDITACI[OÓ]N|FIRMA DIGITAL)\b", sub, maxsplit=1, flags=re.I)[0]
    found = []
    for line in sub.splitlines():
        for fecha in dates_in(line):
            found.append({'fecha':fecha, 'convocatoria':''})
    # En PDFs con columnas desplazadas, la fecha puede estar junta al título.
    if not found:
        found = [{'fecha':d, 'convocatoria':''} for d in dates_in(sub)]
    return sorted(found, key=lambda x:x['fecha'])

def parse_pdf(file_bytes, filename):
    doc = fitz.open(stream=file_bytes, filetype='pdf')
    text = '\n'.join(page.get_text(sort=True) for page in doc)
    if len(text.strip()) < 100:
        raise ValueError('No se detectó texto seleccionable: PDF escaneado. Requiere OCR o verificación manual.')
    ced = re.search(r'\b(?:0\d{9}|\d{10})\b', text)
    name = extract_field(text, 'NOMBRES')
    # Some certificates split the name into multiple lines before the next header.
    if not name:
        m = re.search(r'NOMBRES\s*:\s*\n?\s*([^\n]+)', text, re.I)
        name = m.group(1).strip() if m else ''
    acc = re.search(r'\b(?:MDG|MDI)-[A-Z0-9-]+\b', text, re.I)
    sections = list(re.finditer(r'Nivel\s*([12])\s*:\s*Vigilancia\s*[^\n]*', text, re.I))
    course = {}
    hist = []
    for idx, m in enumerate(sections):
        level = m.group(1)
        end = sections[idx+1].start() if idx+1 < len(sections) else len(text)
        segment = text[m.end():end]
        # Prevent footer from being picked up as part of last course.
        segment = re.split(r'IMPORTANTE\s*\.-|IMPORTANTE\s*-',segment, flags=re.I)[0]
        initial, call = parse_course(segment)
        reents = parse_retrain(segment)
        reents = sorted(reents, key=lambda z:z['fecha'])
        course[level] = (initial,call,reents)
        if initial:
            hist.append({'Cédula': ced.group(0) if ced else '', 'Nombre':name,'Nivel':'Nivel '+level,'Tipo':'Curso inicial','Fecha':initial,'Convocatoria':call,'Archivo':filename})
        for item in reents:
            hist.append({'Cédula': ced.group(0) if ced else '', 'Nombre':name,'Nivel':'Nivel '+level,'Tipo':'Reentrenamiento','Fecha':item['fecha'],'Convocatoria':item['convocatoria'],'Archivo':filename})
    row = {'Cédula':ced.group(0) if ced else '', 'Nombres y apellidos':name,
           'Acreditación':acc.group(0) if acc else '',
           'Nivel 1':course.get('1',('','',''))[0],
           'Últ. reent. N1':course.get('1',('','',[]))[2][-1]['fecha'] if course.get('1',('','',[]))[2] else '',
           'Nivel 2':course.get('2',('','',''))[0],
           'Últ. reent. N2':course.get('2',('','',[]))[2][-1]['fecha'] if course.get('2',('','',[]))[2] else '',
           'Archivo PDF':filename,
           'Observaciones':'', 'Revisado':False}
    if not ced or not name: row['Observaciones']='Revisar cédula/nombre extraídos'
    if not sections: row['Observaciones']='No se detectaron secciones Nivel 1 o Nivel 2'
    return row,hist

def add_24_months(value):
    try:
        return (pd.Timestamp(value) + pd.DateOffset(months=PERIODO)).date()
    except (ValueError, TypeError, OverflowError):
        return None

def get_state(due, today):
    if due is None:
        return 'REVISAR', ''
    remaining = (due - today).days
    if remaining < 0:
        return 'VENCIDO', remaining
    if remaining <= 30:
        return 'CRÍTICO', remaining
    if remaining <= 60:
        return 'POR VENCER', remaining
    return 'AL DÍA', remaining

def retraining_status(initial, retraining, today):
    """Control bianual independiente para reentrenamiento de cada nivel.

    El primer plazo se obtiene del curso del nivel correspondiente; los
    siguientes se cuentan desde el último reentrenamiento acreditado.
    Nunca se usa la fecha del otro nivel ni se modifica la expiración N2.
    """
    initial_date = iso_date(initial)
    retraining_date = iso_date(retraining)
    if not initial_date:
        return 'SIN DATOS', '', '', 'Nivel sin curso registrado'
    base = retraining_date or initial_date
    due = add_24_months(base)
    state, remaining = get_state(due, today)
    note = 'No se encontró fecha de reentrenamiento en el PDF; vencimiento calculado desde el curso, pendiente de verificar' if not retraining_date else ''
    if retraining_date and retraining_date < initial_date:
        state = 'REVISAR'
        note = 'El reentrenamiento es anterior al curso: verificar fechas'
    if retraining_date and pd.Timestamp(retraining_date).date() > today:
        state = 'REVISAR'
        note = 'Fecha de reentrenamiento futura: verificar'
    return state, due.isoformat() if due else '', remaining, note

def enrich(df):
    today = date.today()
    records = []
    for _, row in df.iterrows():
        r = row.to_dict()
        warnings = []
        for level in (1, 2):
            state, due, days, note = retraining_status(
                r.get(f'Nivel {level}', ''), r.get(f'Últ. reent. N{level}', ''), today
            )
            r[f'Próx. reent. N{level}'] = due
            r[f'Estado reent. N{level}'] = state
            r[f'Días reent. N{level}'] = days
            if note and r.get(f'Nivel {level}', ''):
                warnings.append(f'N{level}: {note}')
        # La vigencia del curso N2 corre desde SU EMISIÓN; ningún
        # reentrenamiento cambia la fecha de vencimiento del curso.
        initial_n2 = iso_date(r.get('Nivel 2', ''))
        n2_due = add_24_months(initial_n2) if initial_n2 else None
        if n2_due:
            n2_state, n2_days = get_state(n2_due, today)
        else:
            n2_state, n2_days = 'SIN DATOS', ''
        r['Vencimiento curso N2'] = n2_due.isoformat() if n2_due else ''
        r['Estado curso N2'] = n2_state
        r['Días curso N2'] = n2_days
        levels = [r['Estado reent. N1'], r['Estado reent. N2']]
        if 'VENCIDO' in levels:
            general = 'VENCIDO'
        elif 'REVISAR' in levels:
            general = 'REVISAR'
        elif 'CRÍTICO' in levels:
            general = 'CRÍTICO'
        elif 'POR VENCER' in levels:
            general = 'POR VENCER'
        elif all(x == 'AL DÍA' for x in levels):
            general = 'AL DÍA'
        else:
            general = 'VERIFICAR'
        r['Estado reentrenamientos'] = general
        for level in (1, 2):
            r[f'Fecha reent. N{level} verificada'] = 'EXTRAÍDA DEL PDF / REVISAR' if iso_date(r.get(f'Últ. reent. N{level}', '')) else 'NO CONSTA EN EXTRACCIÓN'
        # Advertencia aparte: no confundir estado del curso N2 con
        # reentrenamiento N2, que tiene su propio calendario.
        if n2_state == 'VENCIDO':
            warnings.append('Curso N2 vencido (control independiente del reentrenamiento)')
        notes = str(r.get('Observaciones', '')).strip()
        r['Observaciones'] = '; '.join(filter(None, [notes, *warnings]))
        records.append(r)
    return pd.DataFrame(records)

def excel_bytes(df, hist):
    main = ['Cédula', 'Nombres y apellidos',
            'Nivel 1', 'Últ. reent. N1', 'Próx. reent. N1', 'Estado reent. N1',
            'Nivel 2', 'Vencimiento curso N2', 'Estado curso N2',
            'Últ. reent. N2', 'Próx. reent. N2', 'Estado reent. N2',
            'Estado reentrenamientos', 'Fecha reent. N1 verificada', 'Fecha reent. N2 verificada', 'Observaciones', 'Archivo PDF']
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='dd/mm/yyyy') as writer:
        df[main].to_excel(writer, sheet_name='RESUMEN', index=False)
        hist.to_excel(writer, sheet_name='HISTORIAL', index=False)
        book = writer.book
        fmt_head = book.add_format({'bold': True, 'font_color': 'white', 'bg_color': '#17365D', 'text_wrap': True, 'valign': 'vcenter'})
        alert = book.add_format({'bg_color': '#F8D7DA', 'font_color': '#8A1C22'})
        soon = book.add_format({'bg_color': '#FFF2CC'})
        fine = book.add_format({'bg_color': '#E2F0D9'})
        for name, data in [('RESUMEN', df[main]), ('HISTORIAL', hist)]:
            ws = writer.sheets[name]
            ws.freeze_panes(1, 2)
            ws.autofilter(0, 0, max(len(data), 1), len(data.columns)-1)
            ws.set_row(0, 34, fmt_head)
            ws.set_landscape()
            ws.fit_to_pages(1, 0)
            for col, k in enumerate(data.columns):
                width = 39 if 'Nombres' in k or 'Observaciones' in k else (27 if 'Archivo' in k else 19)
                ws.set_column(col, col, width)
            if name == 'RESUMEN' and len(data):
                for col in (5, 8, 11, 12):
                    area = (1, col, len(data), col)
                    for phrase, fmt in [('VENCIDO', alert), ('CRÍTICO', soon), ('POR VENCER', soon), ('AL DÍA', fine)]:
                        ws.conditional_format(*area, {'type': 'text', 'criteria': 'containing', 'value': phrase, 'format': fmt})
    return output.getvalue()

st.info('**Controles separados:** el curso Nivel 2 vence 24 meses después de su emisión, sin que el reentrenamiento lo renueve. Los reentrenamientos de Nivel 1 y Nivel 2 se controlan individualmente cada 24 meses desde el último reentrenamiento registrado (o desde el curso si aún no hay ninguno). Verifica los datos oficiales en SICOSEP.')
files=st.file_uploader('Sube certificados de acreditación en PDF (puedes seleccionar muchos a la vez)',type=['pdf'],accept_multiple_files=True)
if files:
    # No persistence: each upload batch is processed in memory only.
    rows=[]; histories=[]; errors=[]
    for file in files:
        try:
            r,h=parse_pdf(file.getvalue(),file.name)
            rows.append(r); histories.extend(h)
        except Exception as exc:
            errors.append(f'{file.name}: {exc}')
    for e in errors: st.warning(e)
    if rows:
        raw=pd.DataFrame(rows)
        duplicates=raw[raw.duplicated('Cédula',keep=False) & raw['Cédula'].ne('')]
        if len(duplicates): st.warning('Hay cédulas repetidas: revisa si son versiones distintas del certificado antes de exportar.')
        st.subheader('1. Revisa y corrige los datos')
        st.caption('Las celdas son editables. Las columnas Últ. reent. N1/N2 muestran las fechas detectadas en el PDF. Si están vacías, no se pudo confirmar una fecha: revisa el PDF antes de asumir que está vencido.')
        edited=st.data_editor(raw,num_rows='dynamic',hide_index=True,use_container_width=True,key='extracted')
        result=enrich(edited)
        st.subheader('2. Control resumido')
        k1,k2,k3,k4=st.columns(4)
        k1.metric('Guardias cargados',len(result))
        k2.metric('Vencidos',int((result['Estado reentrenamientos']=='VENCIDO').sum()))
        k3.metric('Por vencer / críticos',int(result['Estado reentrenamientos'].isin(['CRÍTICO','POR VENCER']).sum()))
        k4.metric('Por verificar',int(result['Estado reentrenamientos'].isin(['VERIFICAR','REVISAR']).sum()))
        main=['Cédula', 'Nombres y apellidos', 'Nivel 1', 'Últ. reent. N1', 'Próx. reent. N1', 'Estado reent. N1', 'Nivel 2', 'Vencimiento curso N2', 'Estado curso N2', 'Últ. reent. N2', 'Próx. reent. N2', 'Estado reent. N2', 'Estado reentrenamientos', 'Fecha reent. N1 verificada', 'Fecha reent. N2 verificada', 'Observaciones']
        st.caption('Un reentrenamiento de 2026 cambia el próximo reentrenamiento a 2028. Si el PDF no muestra su fecha, la alerta es PROVISIONAL hasta verificar el documento. El curso N2 se controla aparte.')
        st.dataframe(result[main],hide_index=True,use_container_width=True)
        hist_df=pd.DataFrame(histories,columns=['Cédula','Nombre','Nivel','Tipo','Fecha','Convocatoria','Archivo'])
        st.download_button('⬇️ Descargar Excel resumido + historial',data=excel_bytes(result,hist_df),file_name=f'SICOSEP_CENASE_{date.today():%Y%m%d}.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        st.caption('Esta herramienta no consulta directamente SICOSEP ni valida oficialmente acreditaciones. Solo procesa los PDF cargados; los escaneados sin texto requieren OCR.')
else:
    st.write('Carga uno o varios certificados para comenzar. Los datos se procesan en la sesión; la aplicación no guarda automáticamente una base permanente.')
