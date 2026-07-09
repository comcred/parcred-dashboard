from bs4 import BeautifulSoup
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
        from playwright.sync_api import sync_playwright
        
        usuario = os.environ.get('BANKSOFT_USER', '')
        senha   = os.environ.get('BANKSOFT_PASS', '')
        if not usuario or not senha:
            return {'error': 'Credenciais não configuradas'}

        hoje   = datetime.now(BR_TZ)
        dt_ini = '01/05/2026'
        dt_fim = hoje.strftime('%d/%m/%Y')
        
        BASE = 'https://parcred.banksofttecnologia.com.br/AppConsig'

        # Install chromium if not present
        import subprocess as sp
        sp.run(['python', '-m', 'playwright', 'install', 'chromium'], 
               capture_output=True, timeout=180)
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu',
                      '--disable-setuid-sandbox','--single-process']
            )
            page = browser.new_page()
            page.set_default_timeout(30000)

            # Login
            page.goto(f'{BASE}/Login/ICLogin', wait_until='networkidle')
            page.fill('input[name="txtUsuario$CAMPO"]', usuario)
            page.fill('input[name="txtSenha$CAMPO"]', senha)
            page.click('a:has-text("Acessar"), input[type="submit"], button[type="submit"]')
            page.wait_for_load_state('networkidle')

            # Navigate to report
            page.goto(f'{BASE}/Pages/Relatorios/ICRLProducaoAnalitico', wait_until='networkidle')
            page.wait_for_timeout(2000)

            # Fill dates
            page.fill('input[name="ctl00$Cph$txtFaixaData$edit1$CAMPO"]', dt_ini)
            page.fill('input[name="ctl00$Cph$txtFaixaData$edit2$CAMPO"]', dt_fim)

            # Uncheck all status checkboxes then check only Integrado
            for chk in ['chkStatusSimulacao','chkStatusCadastro','chkStatusAndamento',
                        'chkStatusPendente','chkStatusAprovado','chkStatusLiberado','chkStatusReprovado']:
                try:
                    cb = page.locator(f'input[name="ctl00$Cph${chk}"]')
                    if cb.is_checked():
                        cb.uncheck()
                except: pass
            
            try:
                cb_int = page.locator('input[name="ctl00$Cph$chkStatusIntegrado"]')
                if not cb_int.is_checked():
                    cb_int.check()
            except: pass

            # Download CSV
            with page.expect_download(timeout=60000) as dl:
                page.click('a:has-text("Exportar CSV")')
            
            download = dl.value
            csv_bytes = download.read_bytes()
            browser.close()

            # Parse CSV
            import csv, io
            text   = csv_bytes.decode('utf-8-sig', errors='replace')
            reader = csv.DictReader(io.StringIO(text), delimiter=';')
            rows   = [dict(r) for r in reader]
            
            if not rows or len(rows[0]) < 3:
                return {'error': 'CSV vazio ou inválido', 'preview': text[:200]}

            return {
                'rows': rows,
                'total': len(rows),
                'periodo': f'{dt_ini} a {dt_fim}',
                'atualizado': hoje.strftime('%d/%m/%Y %H:%M')
            }

    except Exception as e:
        import traceback
        return {'error': str(e), 'traceback': traceback.format_exc()[-800:]}


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
