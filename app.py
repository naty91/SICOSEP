import io
import re
import unicodedata
from datetime import date, datetime

import fitz
import pandas as pd
import streamlit as st

st.set_page_config(page_title='SICOSEP | CENASE', page_icon='🛡️', layout='wide')
st.title('🛡️ CENASE | Control de capacitaciones SICOSEP')
st.caption('Carga certificados PDF, revisa los datos extraídos y descarga un Excel resumido con un único reentrenamiento aplicable a ambos niveles.')

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

def parse_retrain_all(text):
    """Detecta un reentrenamiento GENERAL, aplicable a ambos niveles.

    Busca únicamente en bloques rotulados REENTRENAMIENTO(S); no confunde
    la emisión del certificado ni las fechas de los cursos con el reentrenamiento.
    """
    matches = list(re.finditer(r"\bRE\s*ENTRENAMIENTOS?\b", normalize(text), flags=re.I))
    if not matches:
        return []
    found = []
    for match in matches:
        block = text[match.end():]
        block = re.split(
            r"(?:^|\n)\s*(?:IMPORTANTE|OBSERVACIONES|FIRMA|PAG\.?\s*\d+|NIVEL\s*[12]\s*:|CURSOS APROBADOS|N[UÚ]MERO DE ACREDITACI[OÓ]N)\b",
            block, maxsplit=1, flags=re.I|re.M
        )[0]
        # Limitar el ámbito ante certificados con extracción desordenada.
        block = block[:2500]
        for item in dates_in(block):
            if item not in found:
                found.append(item)
    return sorted(found)

def parse_pdf(file_bytes, filename):
    doc = fitz.open(stream=file_bytes, filetype='pdf')
    text = '\n'.join(page.get_text(sort=True) for page in doc)
    if len(text.strip()) < 100:
        raise ValueError('PDF escaneado sin texto reconocible; requiere revisión manual u OCR.')
    ced = re.search(r'\b(?:0\d{9}|\d{10})\b', text)
    name = extract_field(text, 'NOMBRES')
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
        segment = re.split(r'IMPORTANTE\s*\.-|IMPORTANTE\s*-|RE\s*ENTRENAMIENTO',segment, flags=re.I)[0]
        initial, call = parse_course(segment)
        course[level] = (initial,call)
        if initial:
            hist.append({'Cédula': ced.group(0) if ced else '', 'Nombre':name,'Nivel':'Nivel '+level,'Tipo':'Curso inicial','Fecha':initial,'Convocatoria':call,'Archivo':filename})
    trainings = parse_retrain_all(text)
    latest = trainings[-1] if trainings else ''
    for dt in trainings:
        hist.append({'Cédula':ced.group(0) if ced else '', 'Nombre':name,'Nivel':'Ambos niveles','Tipo':'Reentrenamiento general','Fecha':dt,'Convocatoria':'','Archivo':filename})
    row = {'Cédula': ced.group(0) if ced else '', 'Nombres y apellidos':name,
           'Acreditación':acc.group(0) if acc else '',
           'Nivel 1':course.get('1',('',''))[0],
           'Nivel 2':course.get('2',('',''))[0],
           'Último reentrenamiento (N1 y N2)': latest,
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

def enrich(df):
    today = date.today()
    records = []
    for _, row in df.iterrows():
        r = row.to_dict()
        notes = []
        training = iso_date(r.get('Último reentrenamiento (N1 y N2)', ''))
        levels = [lvl for lvl in (1,2) if iso_date(r.get(f'Nivel {lvl}', ''))]
        r['Último reentrenamiento (N1 y N2)'] = training
        if training:
            if pd.Timestamp(training).date() > today:
                due = ''
                state = 'REVISAR'
                notes.append('Fecha de reentrenamiento futura: revisar documento')
            else:
                due_obj = add_24_months(training)
                due = due_obj.isoformat() if due_obj else ''
                state, _ = get_state(due_obj, today)
            r['Fecha base del control'] = training
            r['Origen fecha reentrenamiento'] = 'ÚLTIMO REENTRENAMIENTO DEL PDF'
        else:
            # Si no existe reentrenamiento, se calcula desde la fecha del Nivel 2.
            base = iso_date(r.get('Nivel 2', ''))
            r['Fecha base del control'] = base
            r['Origen fecha reentrenamiento'] = 'NIVEL 2 (SIN REENTRENAMIENTO)' if base else 'SIN FECHA DISPONIBLE'
            if base:
                due_obj = add_24_months(base)
                due = due_obj.isoformat() if due_obj else ''
                state, _ = get_state(due_obj, today)
                notes.append('Sin reentrenamiento en PDF: vencimiento calculado desde Nivel 2')
            else:
                due = ''
                state = 'SIN DATOS'
                notes.append('No se registra reentrenamiento ni fecha de Nivel 2')
        r['Próximo reentrenamiento (N1 y N2)'] = due
        r['Estado reentrenamiento (N1 y N2)'] = state
        for lvl in (1,2):
            r[f'Estado reent. N{lvl}'] = state if lvl in levels else 'SIN NIVEL'
        initial_n2 = iso_date(r.get('Nivel 2',''))
        n2_due = add_24_months(initial_n2) if initial_n2 else None
        n2_state, _ = get_state(n2_due, today) if n2_due else ('SIN DATOS','')
        r['Vencimiento curso N2'] = n2_due.isoformat() if n2_due else ''
        r['Estado curso N2'] = n2_state
        if n2_state == 'VENCIDO' and training:
            notes.append('Fecha inicial N2 supera 24 meses; estado general se evalúa con el reentrenamiento más reciente')
        r['Estado general'] = state  # La última actualización manda; N2 queda como control informativo separado.
        r['Observaciones'] = '; '.join(filter(None,[str(r.get('Observaciones','')).strip(), *notes]))
        records.append(r)
    return pd.DataFrame(records)

MAIN_COLUMNS = [
    'Cédula', 'Nombres y apellidos', 'Nivel 1', 'Nivel 2',
    'Vencimiento curso N2', 'Estado curso N2',
    'Último reentrenamiento (N1 y N2)', 'Próximo reentrenamiento (N1 y N2)',
    'Estado reent. N1', 'Estado reent. N2', 'Estado reentrenamiento (N1 y N2)',
    'Estado general', 'Fecha base del control', 'Origen fecha reentrenamiento', 'Observaciones', 'Archivo PDF'
]

def excel_bytes(df, hist):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='dd/mm/yyyy') as writer:
        df[MAIN_COLUMNS].to_excel(writer, sheet_name='RESUMEN', index=False)
        hist.to_excel(writer, sheet_name='HISTORIAL', index=False)
        book = writer.book
        head = book.add_format({'bold':True,'font_color':'white','bg_color':'#17365D','text_wrap':True,'valign':'vcenter'})
        alert = book.add_format({'bg_color':'#F8D7DA','font_color':'#8A1C22'})
        soon = book.add_format({'bg_color':'#FFF2CC'})
        fine = book.add_format({'bg_color':'#E2F0D9'})
        for name, data in [('RESUMEN',df[MAIN_COLUMNS]),('HISTORIAL',hist)]:
            ws=writer.sheets[name]
            ws.freeze_panes(1,2)
            ws.autofilter(0,0,max(len(data),1),len(data.columns)-1)
            ws.set_row(0,44,head)
            ws.set_landscape(); ws.fit_to_pages(1,0)
            for col,key in enumerate(data.columns):
                ws.set_column(col,col,43 if key in ('Nombres y apellidos','Observaciones') else 26 if 'reentrenamiento' in key.lower() else 20)
            if name=='RESUMEN' and len(data):
                for col,key in enumerate(data.columns):
                    if key.startswith('Estado'):
                        for phrase, fmt in [('VENCIDO',alert),('CRÍTICO',soon),('POR VENCER',soon),('AL DÍA',fine)]:
                            ws.conditional_format(1,col,len(data),col,{'type':'text','criteria':'containing','value':phrase,'format':fmt})
    return output.getvalue()

st.info('**Regla del control:** se toma el reentrenamiento MÁS RECIENTE del PDF y se aplica a ambos niveles; vencimiento = última fecha + 24 meses. Si no constan reentrenamientos, se usa la fecha de registro de Nivel 2 + 24 meses. El control histórico del curso N2 se muestra aparte y NO reemplaza el estado general cuando existe un reentrenamiento posterior.')
files=st.file_uploader('Sube certificados SICOSEP en PDF (admite carga múltiple)',type=['pdf'],accept_multiple_files=True)
if files:
    rows=[]; histories=[]; errors=[]
    for file in files:
        try:
            r,h=parse_pdf(file.getvalue(),file.name)
            rows.append(r); histories.extend(h)
        except Exception as exc:
            errors.append(f'{file.name}: {exc}')
    for error in errors: st.warning(error)
    if rows:
        raw=pd.DataFrame(rows)
        if raw.duplicated('Cédula',keep=False).any():
            st.warning('Cédulas duplicadas: revisa los archivos antes de exportar.')
        st.subheader('1. Validar fechas identificadas en PDF')
        st.caption('Una sola columna de reentrenamiento para ambos niveles. Puedes editarla si el PDF contiene una fecha que no fue reconocida automáticamente.')
        edited=st.data_editor(raw,num_rows='dynamic',hide_index=True,use_container_width=True,key='extracted')
        result=enrich(edited)
        st.subheader('2. Resultados y alertas')
        k1,k2,k3,k4=st.columns(4)
        k1.metric('Guardias',len(result))
        k2.metric('Reentrenamiento vencido',int((result['Estado reentrenamiento (N1 y N2)']=='VENCIDO').sum()))
        k3.metric('Próximos / críticos',int(result['Estado reentrenamiento (N1 y N2)'].isin(['CRÍTICO','POR VENCER']).sum()))
        k4.metric('Sin datos / revisar',int(result['Estado reentrenamiento (N1 y N2)'].isin(['SIN DATOS','REVISAR']).sum()))
        st.dataframe(result[MAIN_COLUMNS],hide_index=True,use_container_width=True)
        hist_df=pd.DataFrame(histories,columns=['Cédula','Nombre','Nivel','Tipo','Fecha','Convocatoria','Archivo'])
        st.download_button('⬇️ Descargar Excel + historial',data=excel_bytes(result,hist_df),file_name=f'SICOSEP_CENASE_{date.today():%Y%m%d}.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        st.caption('La aplicación analiza únicamente los PDF cargados. No consulta la plataforma oficial SICOSEP.')
else:
    st.write('Carga tus certificados PDF para generar el resumen.')
