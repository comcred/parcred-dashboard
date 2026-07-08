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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        })

        # GET login page to get tokens/cookies
        r = session.get(f'{BANKSOFT_BASE}/Login/ICLogin', timeout=30)
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, 'lxml')
        
        # Get hidden fields (CSRF tokens etc)
        form_data = {}
        for inp in soup.find_all('input', type='hidden'):
            if inp.get('name'):
                form_data[inp['name']] = inp.get('value', '')
        
        # Find username/password field names
        user_field = 'login'
        pass_field = 'senha'
        for inp in soup.find_all('input'):
            name = (inp.get('name') or '').lower()
            itype = (inp.get('type') or '').lower()
            if itype == 'text' or 'usu' in name or 'login' in name or 'user' in name:
                user_field = inp.get('name', user_field)
            if itype == 'password' or 'sen' in name or 'pass' in name:
                pass_field = inp.get('name', pass_field)
        
        form_data[user_field] = usuario
        form_data[pass_field] = senha

        # POST login
        r2 = session.post(f'{BANKSOFT_BASE}/Login/ICLogin', data=form_data, 
                         allow_redirects=True, timeout=30)
        
        # Debug: capture login response info
        login_debug = {
            'url_after_login': r2.url,
            'status': r2.status_code,
            'has_menu': 'menu' in r2.url.lower() or 'home' in r2.url.lower(),
            'page_title': '',
            'form_fields_found': list(form_data.keys()),
            'user_field_used': user_field,
            'pass_field_used': pass_field,
        }
        try:
            soup_debug = BeautifulSoup(r2.text, 'lxml')
            title = soup_debug.find('title')
            login_debug['page_title'] = title.get_text() if title else ''
            # Check if still on login page
            login_inputs = soup_debug.find_all('input', type='password')
            login_debug['still_on_login'] = len(login_inputs) > 0
            # Get all links from page
            links = [a.get('href','') for a in soup_debug.find_all('a', href=True)][:10]
            login_debug['links'] = links
        except: pass
        
        if login_debug.get('still_on_login') or 'login' in r2.url.lower():
            return {'error': f'Login falhou', 'debug': login_debug}

        # Get report page
        r3 = session.get(f'{BANKSOFT_BASE}/Pages/Relatorio/ICRelatorioProducaoAnalitico', 
                        timeout=30)
        if r3.status_code != 200:
            # Try to find report URL from menu
            soup2 = BeautifulSoup(r2.text, 'lxml')
            links = soup2.find_all('a', href=True)
            report_url = None
            for link in links:
                href = link.get('href','').lower()
                text = link.get_text().lower()
                if 'producao' in href or 'producao' in text or 'analitico' in href:
                    report_url = link['href']
                    break
            if report_url:
                if not report_url.startswith('http'):
                    report_url = BANKSOFT_BASE + '/' + report_url.lstrip('/')
                r3 = session.get(report_url, timeout=30)

        soup3 = BeautifulSoup(r3.text, 'lxml')
        
        # Find form and fill dates + situacao
        form3 = {}
        for inp in soup3.find_all('input'):
            if inp.get('name'):
                form3[inp['name']] = inp.get('value','')
        
        # Set period and situacao
        for inp in soup3.find_all('input', type='text'):
            name = (inp.get('name') or '').lower()
            placeholder = (inp.get('placeholder') or '').lower()
            if 'ini' in name or 'inicio' in name or 'de' == name or 'ini' in placeholder:
                form3[inp['name']] = dt_ini
            elif 'fim' in name or 'ate' in name or 'fim' in placeholder:
                form3[inp['name']] = dt_fim
        
        # Find situacao select
        for sel in soup3.find_all('select'):
            name = (sel.get('name') or '').lower()
            if 'sit' in name or 'status' in name or 'situac' in name:
                form3[sel['name']] = 'INT'

        # Submit and get CSV
        export_url = r3.url
        r4 = session.post(export_url, data={**form3, 'exportar': 'csv', 'formato': 'csv'}, 
                         timeout=60)
        
        # Check if response is CSV
        content_type = r4.headers.get('Content-Type', '')
        if 'csv' in content_type or 'text' in content_type or len(r4.content) > 1000:
            try:
                text = r4.content.decode('utf-8-sig')
                reader = csv.DictReader(io.StringIO(text), delimiter=';')
                rows = [dict(r) for r in reader]
                if rows and len(rows[0]) > 3:
                    return {
                        'rows': rows, 
                        'total': len(rows),
                        'periodo': f'{dt_ini} a {dt_fim}',
                        'atualizado': hoje.strftime('%d/%m/%Y %H:%M')
                    }
            except: pass
        
        return {'error': 'Não foi possível baixar o CSV. O sistema pode ter mudado o layout.', 
                'debug_url': r3.url, 'debug_status': r4.status_code}

    except Exception as e:
        return {'error': str(e)}

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
