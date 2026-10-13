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

def parse_retrain(section):
    """Collect all visible dated retraining entries within the level's own section."""
    if not re.search(r'Reentrenamientos\s*:', section, re.I):
        return []
    sub = re.split(r'Reentrenamientos\s*:', section, flags=re.I, maxsplit=1)[-1]
    found = []
    for match in re.finditer(r'(\d{4}-\d{2}-\d{2})', sub):
        nearby = sub[max(0, match.start()-260):match.start()]
        calls = re.findall(r'\b[\w-]*REENT[\w-]*\b', nearby, re.I)
        found.append({'fecha': match.group(1), 'convocatoria': calls[-1] if calls else ''})
    return found

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

def status(initial, reent, today):
    ini, re = iso_date(initial), iso_date(reent)
    if not ini:
        return 'SIN DATOS','', '', 'Verificar el curso de este nivel' if re else 'Nivel no registrado'
    base = re or ini
    try:
        due = (pd.Timestamp(base) + pd.DateOffset(months=PERIODO)).date()
    except (ValueError,TypeError,OverflowError):
        return 'REVISAR','', '', 'Fecha no válida'
    remaining = (due-today).days
    if remaining < 0: state='VENCIDO'
    elif remaining <= 30: state='CRÍTICO'
    elif remaining <= 60: state='POR VENCER'
    else: state='AL DÍA'
    return state,due.isoformat(), remaining, 'No consta reentrenamiento; verificar en SICOSEP' if not re else ''

def enrich(df):
    records=[]
    for _, row in df.iterrows():
        r=row.to_dict()
        warnings=[]
        for level in (1,2):
            state, due, days, note = status(r.get(f'Nivel {level}',''),r.get(f'Últ. reent. N{level}',''),date.today())
            r[f'Próx. N{level}']=due
            r[f'Estado N{level}']=state
            r[f'Días N{level}']=days
            if note and r.get(f'Nivel {level}',''): warnings.append(f'N{level}: {note}')
        valid=[r['Estado N1'],r['Estado N2']]
        if 'VENCIDO' in valid: general='VENCIDO'
        elif 'CRÍTICO' in valid: general='CRÍTICO'
        elif 'POR VENCER' in valid: general='POR VENCER'
        elif 'REVISAR' in valid: general='REVISAR'
        elif 'AL DÍA' in valid and ('SIN DATOS' not in valid): general='AL DÍA'
        else: general='VERIFICAR'
        r['Estado general']=general
        notes = str(r.get('Observaciones','')).strip()
        r['Observaciones']='; '.join(filter(None, [notes,*warnings]))
        records.append(r)
    return pd.DataFrame(records)

def excel_bytes(df,hist):
    main = ['Cédula','Nombres y apellidos','Nivel 1','Últ. reent. N1','Próx. N1','Estado N1',
            'Nivel 2','Últ. reent. N2','Próx. N2','Estado N2','Estado general','Observaciones','Archivo PDF']
    output=io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter', datetime_format='dd/mm/yyyy') as writer:
        df[main].to_excel(writer,sheet_name='RESUMEN',index=False)
        hist.to_excel(writer,sheet_name='HISTORIAL',index=False)
        book=writer.book
        fmt_head=book.add_format({'bold':True,'font_color':'white','bg_color':'#17365D','text_wrap':True,'valign':'vcenter'})
        alert=book.add_format({'bg_color':'#F8D7DA','font_color':'#8A1C22'})
        soon=book.add_format({'bg_color':'#FFF2CC'})
        fine=book.add_format({'bg_color':'#E2F0D9'})
        for name, data in [('RESUMEN',df[main]),('HISTORIAL',hist)]:
            ws=writer.sheets[name]
            ws.freeze_panes(1,2)
            ws.autofilter(0,0,max(len(data),1),len(data.columns)-1)
            ws.set_row(0,32,fmt_head)
            for col,k in enumerate(data.columns):
                width=39 if 'Nombres' in k or 'Observaciones' in k else (27 if 'Archivo' in k else 18)
                ws.set_column(col,col,width)
            if name=='RESUMEN' and len(data):
                for col in (5,9,10):
                    letter=chr(65+col)
                    area=(1,col,len(data),col)
                    ws.conditional_format(*area, {'type':'text','criteria':'containing','value':'VENCIDO','format':alert})
                    ws.conditional_format(*area, {'type':'text','criteria':'containing','value':'CRÍTICO','format':soon})
                    ws.conditional_format(*area, {'type':'text','criteria':'containing','value':'AL DÍA','format':fine})
    return output.getvalue()

st.info('Se calculan alertas de gestión a 24 meses por cada nivel. **No se considera que un reentrenamiento de N1 renueve automáticamente N2 ni viceversa.** Confirma siempre la situación oficial en SICOSEP.')
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
        st.caption('Las celdas se pueden editar. Deja vacía la fecha de reentrenamiento cuando NO conste en el PDF.')
        edited=st.data_editor(raw,num_rows='dynamic',hide_index=True,use_container_width=True,key='extracted')
        result=enrich(edited)
        st.subheader('2. Control resumido')
        k1,k2,k3,k4=st.columns(4)
        k1.metric('Guardias cargados',len(result))
        k2.metric('Vencidos',int((result['Estado general']=='VENCIDO').sum()))
        k3.metric('Por vencer / críticos',int(result['Estado general'].isin(['CRÍTICO','POR VENCER']).sum()))
        k4.metric('Por verificar',int(result['Estado general'].isin(['VERIFICAR','REVISAR']).sum()))
        main=['Cédula','Nombres y apellidos','Nivel 1','Últ. reent. N1','Próx. N1','Estado N1','Nivel 2','Últ. reent. N2','Próx. N2','Estado N2','Estado general','Observaciones']
        st.dataframe(result[main],hide_index=True,use_container_width=True)
        hist_df=pd.DataFrame(histories,columns=['Cédula','Nombre','Nivel','Tipo','Fecha','Convocatoria','Archivo'])
        st.download_button('⬇️ Descargar Excel resumido + historial',data=excel_bytes(result,hist_df),file_name=f'SICOSEP_CENASE_{date.today():%Y%m%d}.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        st.caption('Esta herramienta no consulta directamente SICOSEP ni valida oficialmente acreditaciones. Solo procesa los PDF cargados; los escaneados sin texto requieren OCR.')
else:
    st.write('Carga uno o varios certificados para comenzar. Los datos se procesan en la sesión; la aplicación no guarda automáticamente una base permanente.')
