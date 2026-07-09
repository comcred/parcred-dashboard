from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials
import os, json
from datetime import datetime
import pytz

app = Flask(__name__, static_folder='static')
CORS(app)

SHEET_ID = '1na0NwEEN8khztM53d_c33yt_Wgnrc4At2akqYq2g5zA'
SCOPES   = ['https://www.googleapis.com/auth/spreadsheets']
BR_TZ    = pytz.timezone('America/Sao_Paulo')
EQUIPE   = ['Gustavo Henrique','Diego Demétrio','Felício Lemos',"Rafael Sant'Anna"]
STATUS_LIST = ['FILA','CONSULTA DE DADOS','CONSULTA TÉCNICA PROCESSADORA','JURÍDICO','COMITÊ EXECUTIVO','PENDÊNCIA COMITÊ','CREDENCIAMENTO','DOCS ENVIADOS','DOCS EM ANÁLISE','ASSINATURA','RUBRICA','CONTRATO PROCESSADORA','CREDENCIADO','IMPEDIDO','CONFLITO IF']
ETAPAS_ALERTA = ['FILA','CONSULTA DE DADOS','CONSULTA TÉCNICA PROCESSADORA','JURÍDICO','COMITÊ EXECUTIVO','PENDÊNCIA COMITÊ','CREDENCIAMENTO','DOCS ENVIADOS','DOCS EM ANÁLISE','ASSINATURA','RUBRICA','CONTRATO PROCESSADORA']
DIAS_ALERTA = 15

import requests as req_lib
import threading
import csv
import io

_banksoft_cache = {'data': None, 'updated': None}
_banksoft_lock  = threading.Lock()

BANKSOFT_BASE = 'https://parcred.banksofttecnologia.com.br/AppConsig'

def buscar_producao_banksoft():
    try:
        usuario = os.environ.get('BANKSOFT_USER', '')
        senha   = os.environ.get('BANKSOFT_PASS', '')
        if not usuario or not senha:
            return {'error': 'Credenciais não configuradas no Render'}

        hoje   = datetime.now(BR_TZ)
        dt_ini = '01/05/2026'
        dt_fim = hoje.strftime('%d/%m/%Y')

        session = req_lib.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })

        # GET login page to capture VIEWSTATE and other hidden fields
        r = session.get('https://parcred.banksofttecnologia.com.br/AppConsig/Login/ICLogin', timeout=30)
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, 'lxml')

        # Capture ALL hidden fields
        form_data = {}
        for inp in soup.find_all('input'):
            name = inp.get('name','')
            if name:
                form_data[name] = inp.get('value','')

        # Set credentials with exact field names
        form_data['txtUsuario$CAMPO'] = usuario
        form_data['txtSenha$CAMPO']   = senha

        # ASP.NET requires __EVENTTARGET for button clicks via PostBack
        # Find the login button
        btn = soup.find('input', type='submit') or soup.find('button', type='submit')
        if btn and btn.get('name'):
            form_data[btn['name']] = btn.get('value','Entrar')
        else:
            # Try to find link button or set EVENTTARGET
            for a in soup.find_all('a', href=True):
                href = a.get('href','')
                if 'doPostBack' in href and ('login' in href.lower() or 'entr' in href.lower() or 'acesso' in href.lower()):
                    import re
                    m = re.search(r"__doPostBack\('([^']+)'", href)
                    if m:
                        form_data['__EVENTTARGET'] = m.group(1)
                        form_data['__EVENTARGUMENT'] = ''

        # POST login
        session.headers.update({
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://parcred.banksofttecnologia.com.br/AppConsig/Login/ICLogin',
            'Origin': 'https://parcred.banksofttecnologia.com.br',
        })

        r2 = session.post(
            'https://parcred.banksofttecnologia.com.br/AppConsig/Login/ICLogin',
            data=form_data,
            allow_redirects=True,
            timeout=30
        )

        soup2 = BeautifulSoup(r2.text, 'lxml')
        still_login = len(soup2.find_all('input', type='password')) > 0
        page_title  = (soup2.find('title') or soup2.new_tag('x')).get_text(strip=True)

        if still_login:
            # Try with lnkAcessar or similar button
            form_data['__EVENTTARGET'] = 'lnkAcessar'
            form_data['__EVENTARGUMENT'] = ''
            r2 = session.post(
                'https://parcred.banksofttecnologia.com.br/AppConsig/Login/ICLogin',
                data=form_data,
                allow_redirects=True,
                timeout=30
            )
            soup2 = BeautifulSoup(r2.text, 'lxml')
            still_login = len(soup2.find_all('input', type='password')) > 0
            page_title  = (soup2.find('title') or soup2.new_tag('x')).get_text(strip=True)

        if still_login:
            # Get all links/buttons to debug
            btns = [str(b) for b in soup.find_all(['input','button','a']) if b.get('type') in ['submit','button'] or 'doPostBack' in str(b)]
            return {'error': 'Login ainda falhou', 'debug': {
                'page_title': page_title,
                'url': r2.url,
                'buttons_found': btns[:5],
                'form_fields_sent': list(form_data.keys())
            }}

        # Login OK! Now navigate to report
        BASE = 'https://parcred.banksofttecnologia.com.br/AppConsig'
        
        # After login try to access report directly
        # First: navigate to menu to get session cookies validated
        session.get(f'{BASE}/Pages/Menu/ICMenu', timeout=20)
        
        # Try all possible report URLs
        PROD_CANDIDATES = [
            f'{BASE}/Pages/Relatorios/ICRLProducaoAnalitico',
            f'{BASE}/Pages/Relatorio/ICRLProducaoAnalitico',
            f'{BASE}/Pages/Relatorios/ICRelatorioProducaoAnalitico',
            f'{BASE}/Pages/Relatorio/ICRelatorioProducaoAnalitico',
        ]
        
        prod_url = None
        soup4 = None
        for candidate in PROD_CANDIDATES:
            rc = session.get(candidate, timeout=20)
            if rc.status_code == 200 and 'login' not in rc.url.lower():
                soup_c = BeautifulSoup(rc.text, 'lxml')
                inputs = soup_c.find_all('input', type='text')
                selects = soup_c.find_all('select')
                title = (soup_c.find('title') or soup_c.new_tag('x')).get_text(strip=True)
                if len(inputs) >= 1 or len(selects) >= 1:
                    prod_url = rc.url
                    soup4 = soup_c
                    break
        
        if not prod_url:
            # Debug: show what pages are accessible
            debug_pages = {}
            for candidate in PROD_CANDIDATES[:3]:
                rc = session.get(candidate, timeout=10)
                debug_pages[candidate.split('/')[-1]] = {
                    'status': rc.status_code,
                    'url': rc.url,
                    'title': BeautifulSoup(rc.text,'lxml').find('title') and BeautifulSoup(rc.text,'lxml').find('title').get_text(strip=True)
                }
            return {'error': 'Página de relatório não encontrada', 
                    'debug': {'post_login_url': r2.url, 'pages_tried': debug_pages}}

        # Fill form
        form4 = {}
        for inp in soup4.find_all('input'):
            if inp.get('name'):
                form4[inp['name']] = inp.get('value','')

        # Fill dates
        for inp in soup4.find_all('input', type='text'):
            name = (inp.get('name') or '').lower()
            ph   = (inp.get('placeholder') or '').lower()
            if any(x in name+ph for x in ['ini','inicio','início','de','from']):
                form4[inp['name']] = dt_ini
            elif any(x in name+ph for x in ['fim','até','ate','to','end']):
                form4[inp['name']] = dt_fim

        # Situação = Integrado
        for sel in soup4.find_all('select'):
            name = (sel.get('name') or '').lower()
            if any(x in name for x in ['sit','status','situac']):
                form4[sel['name']] = 'INT'

        # Find export/CSV button
        export_target = ''
        for inp in soup4.find_all('input'):
            val = (inp.get('value') or '').lower()
            nm  = (inp.get('name') or '').lower()
            if 'csv' in val or 'export' in val or 'csv' in nm:
                export_target = inp.get('name','')
                form4[inp['name']] = inp.get('value','')
                break

        if not export_target:
            for a in soup4.find_all('a', href=True):
                if 'doPostBack' in a.get('href','') and ('csv' in a.get_text().lower() or 'export' in a.get('href','').lower()):
                    import re
                    m = re.search(r"__doPostBack\('([^']+)'", a['href'])
                    if m:
                        form4['__EVENTTARGET'] = m.group(1)
                        form4['__EVENTARGUMENT'] = ''
                        export_target = m.group(1)
                        break

        r5 = session.post(prod_url, data=form4, allow_redirects=True, timeout=60)

        # Parse CSV
        content_type = r5.headers.get('Content-Type','')
        if 'csv' in content_type or 'octet' in content_type or ('text' in content_type and ';' in r5.text[:500]):
            text   = r5.content.decode('utf-8-sig', errors='replace')
            reader = csv.DictReader(io.StringIO(text), delimiter=';')
            rows   = [dict(row) for row in reader]
            if rows:
                return {'rows': rows, 'total': len(rows), 'periodo': f'{dt_ini} a {dt_fim}', 'atualizado': hoje.strftime('%d/%m/%Y %H:%M')}

        # Debug: show all form fields and buttons on report page
        form_fields = {}
        for inp in soup4.find_all('input'):
            nm = inp.get('name','')
            tp = inp.get('type','')
            vl = inp.get('value','')
            if nm and tp not in ['hidden']:
                form_fields[nm] = {'type': tp, 'value': vl[:50]}
        
        selects_found = {}
        for sel in soup4.find_all('select'):
            nm = sel.get('name','')
            if nm:
                opts = [o.get('value','') + '=' + o.get_text(strip=True) for o in sel.find_all('option')]
                selects_found[nm] = opts
        
        buttons_found = []
        for inp in soup4.find_all(['input','button','a']):
            tp = inp.get('type','')
            href = inp.get('href','')
            nm = inp.get('name','')
            vl = inp.get('value','')
            txt = inp.get_text(strip=True)
            if tp in ['submit','button'] or ('doPostBack' in href) or ('csv' in txt.lower()) or ('export' in txt.lower()) or ('csv' in vl.lower()):
                buttons_found.append({'tag': inp.name, 'type': tp, 'name': nm, 'value': vl, 'text': txt, 'href': href[:100]})
        
        return {'error': 'CSV não retornado', 'debug': {
            'content_type': content_type,
            'status': r5.status_code,
            'response_preview': r5.text[:300],
            'prod_url': prod_url,
            'export_target': export_target,
            'form_fields': form_fields,
            'selects': selects_found,
            'buttons': buttons_found,
            'form4_keys': [k for k in form4.keys() if not k.startswith('__')]
        }}

    except Exception as e:
        import traceback
        return {'error': str(e), 'traceback': traceback.format_exc()[-500:]}

@app.route('/api/producao')
def get_producao():
    forcar = request.args.get('forcar','false') == 'true'
    with _banksoft_lock:
        agora = datetime.now(BR_TZ)
        cache_ok = (
            _banksoft_cache['data'] is not None and
            _banksoft_cache['updated'] is not None and
            (agora - _banksoft_cache['updated']).seconds < 3600 and
            not forcar
        )
        if cache_ok:
            return jsonify({**_banksoft_cache['data'], 'cache': True})
        resultado = buscar_producao_banksoft()
        if 'error' not in resultado:
            _banksoft_cache['data']    = resultado
            _banksoft_cache['updated'] = agora
        return jsonify({**resultado, 'cache': False})


def get_client():
    creds_dict = json.loads(os.environ.get('GOOGLE_CREDS','{}'))
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet(name):
    return get_client().open_by_key(SHEET_ID).worksheet(name)

@app.route('/')
def index():
    resp = make_response(send_from_directory('.', 'index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp

@app.route('/api/dados')
def get_dados():
    try:
        master     = get_sheet('Master')
        corbans_ws = get_sheet('Corbans')
        m_vals     = master.get_all_values()
        m_rows     = [r for r in m_vals[2:] if r[0] and r[0].strip()]
        municipios = []
        for r in m_rows:
            def s(i): return r[i].strip() if i < len(r) else ''
            def n(i):
                try: return float(s(i).replace('.','').replace(',','.')) if s(i) else None
                except: return None
            municipios.append({'nome':s(0),'tipo':s(1),'status':s(2).upper(),'capag':s(3),'ifAdm':s(4),'margem':n(5),'colab':n(6),'pot':n(7),'populacao':n(8),'processadora':s(9),'api':s(10),'integ':s(11),'reav':s(12),'contratados':s(13),'parceiro':s(14),'obs':s(21) if len(r)>21 else ''})
        c_vals = corbans_ws.get_all_values()
        corbans_list = []
        for r in [x for x in c_vals[2:] if x[0] and x[0].strip()]:
            def sc(i): return r[i].strip() if i < len(r) else ''
            corbans_list.append({'nome':sc(0),'status':sc(1),'pracas':[sc(i) for i in range(2,7) if sc(i)]})
        alertas  = get_alertas(municipios)
        now_br   = datetime.now(BR_TZ).strftime('%d/%m/%Y %H:%M')
        return jsonify({'municipios':municipios,'corbans':corbans_list,'equipe':EQUIPE,'statusList':STATUS_LIST,'alertas':alertas,'atualizado':now_br})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def get_alertas(municipios):
    try:
        hist   = get_sheet('Histórico')
        h_vals = hist.get_all_values()
        ultima = {}
        for r in h_vals[1:]:
            nome = r[1].strip() if len(r)>1 else ''
            if not nome: continue
            try:
                dt = datetime.strptime(r[0].strip()[:16],'%d/%m/%Y %H:%M')
                if nome not in ultima or dt > ultima[nome]: ultima[nome]=dt
            except: pass
        hoje    = datetime.now(BR_TZ).replace(tzinfo=None)
        alertas = []
        for m in municipios:
            if m['status'] not in ETAPAS_ALERTA: continue
            ult  = ultima.get(m['nome'])
            dias = (hoje-ult).days if ult else 0
            if dias >= DIAS_ALERTA:
                alertas.append({'nome':m['nome'],'status':m['status'],'diasParado':dias,'ultimaAtualizacao':ult.strftime('%d/%m/%Y') if ult else 'Sem registro'})
        alertas.sort(key=lambda x:x['diasParado'],reverse=True)
        return alertas
    except: return []

@app.route('/api/historico')
def get_historico():
    nome = request.args.get('nome','')
    try:
        hist  = get_sheet('Histórico')
        vals  = hist.get_all_values()
        rows  = []
        for r in vals[1:]:
            if len(r)>1 and r[1].strip()==nome.strip():
                rows.append({'data':r[0],'convenio':r[1],'statusAnterior':r[2],'statusNovo':r[3],'observacao':r[4],'responsavel':r[5] if len(r)>5 else ''})
        rows.reverse()
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error':str(e)}),500

@app.route('/api/evoluir', methods=['POST'])
def evoluir():
    try:
        dados  = request.json
        master = get_sheet('Master')
        m_vals = master.get_all_values()
        linha  = -1; status_ant = ''
        for i,r in enumerate(m_vals[2:],start=3):
            if r[0].strip()==dados['nome'].strip(): linha=i; status_ant=r[2].strip(); break
        if linha==-1: return jsonify({'ok':False,'msg':'Convênio não encontrado.'})
        master.update_cell(linha,3,dados['statusNovo'])
        master.update_cell(linha,22,dados['obs'])
        hist  = get_sheet('Histórico')
        agora = datetime.now(BR_TZ).strftime('%d/%m/%Y %H:%M')
        hist.append_row([agora,dados['nome'],status_ant,dados['statusNovo'],dados['obs'],dados['responsavel']])
        return jsonify({'ok':True,'msg':'Status atualizado com sucesso!'})
    except Exception as e:
        return jsonify({'ok':False,'msg':str(e)})

@app.route('/api/adicionar', methods=['POST'])
def adicionar():
    try:
        dados  = request.json
        master = get_sheet('Master')
        m_vals = master.get_all_values()
        for r in m_vals[2:]:
            if r[0].strip().upper()==dados['nome'].upper(): return jsonify({'ok':False,'msg':'Convênio já existe.'})
        nova = [dados.get(k,'') for k in ['nome','tipo','status','capag','ifAdm','margem','colab','pot','pop','proc','api','integ','reav','contrat','parceiro']] + ['','','','','','',dados.get('obs','')]
        master.append_row(nova)
        hist  = get_sheet('Histórico')
        agora = datetime.now(BR_TZ).strftime('%d/%m/%Y %H:%M')
        hist.append_row([agora,dados['nome'],'—',dados['status'],'Adicionado via dashboard. '+dados.get('obs',''),dados.get('responsavel','')])
        return jsonify({'ok':True,'msg':'Convênio adicionado!'})
    except Exception as e:
        return jsonify({'ok':False,'msg':str(e)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port)
