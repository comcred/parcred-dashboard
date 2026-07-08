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

import subprocess
import threading
import csv
import io
from datetime import datetime, timedelta

# Cache for banksoft data
_banksoft_cache = {'data': None, 'updated': None}
_banksoft_lock = threading.Lock()

def instalar_playwright():
    """Instala browsers do playwright se necessário"""
    try:
        subprocess.run(['playwright', 'install', 'chromium', '--with-deps'], 
                      capture_output=True, timeout=120)
    except: pass

def buscar_producao_banksoft():
    """Acessa o sistema Banksoft e baixa o relatório de produção"""
    try:
        from playwright.sync_api import sync_playwright
        
        usuario = os.environ.get('BANKSOFT_USER', '')
        senha   = os.environ.get('BANKSOFT_PASS', '')
        
        if not usuario or not senha:
            return {'error': 'Credenciais não configuradas'}
        
        # Data de início: 01/05/2026, fim: hoje
        hoje     = datetime.now(BR_TZ)
        dt_ini   = '01/05/2026'
        dt_fim   = hoje.strftime('%d/%m/%Y')
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-dev-shm-usage'])
            page    = browser.new_page()
            
            # Login
            page.goto('https://parcred.banksofttecnologia.com.br/AppConsig/Login/ICLogin?ReturnUrl=%2FAppConsig%2FPages%2FMenu%2FICMenu', timeout=30000)
            page.wait_for_load_state('networkidle')
            
            # Preenche usuário e senha
            page.fill('input[name*="usu"], input[id*="usu"], input[type="text"]', usuario)
            page.fill('input[name*="sen"], input[id*="sen"], input[type="password"]', senha)
            page.click('button[type="submit"], input[type="submit"]')
            page.wait_for_load_state('networkidle')
            
            # Navega para Frente de Empréstimo
            page.wait_for_timeout(2000)
            
            # Clica em Relatórios
            page.click('text=Relatórios', timeout=10000)
            page.wait_for_timeout(1000)
            
            # Clica em Produção Analítico
            page.click('text=Produção Analítico', timeout=10000)
            page.wait_for_load_state('networkidle')
            page.wait_for_timeout(2000)
            
            # Preenche período
            campos_data = page.query_selector_all('input[type="text"]')
            # Tenta preencher datas
            for campo in campos_data:
                placeholder = campo.get_attribute('placeholder') or ''
                if 'ini' in placeholder.lower() or 'início' in placeholder.lower() or 'de' in placeholder.lower():
                    campo.fill(dt_ini)
                elif 'fim' in placeholder.lower() or 'até' in placeholder.lower():
                    campo.fill(dt_fim)
            
            # Situação = Integrado
            try:
                page.select_option('select', 'INT')
            except:
                try:
                    page.click('text=Integrado')
                except: pass
            
            # Exportar CSV
            with page.expect_download(timeout=30000) as download_info:
                page.click('text=Exportar, text=CSV, text=Export', timeout=10000)
            
            download  = download_info.value
            csv_bytes = download.read_bytes()
            browser.close()
            
            # Processa CSV
            text = csv_bytes.decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(text), delimiter=';')
            rows = list(reader)
            
            return {'rows': rows, 'total': len(rows), 'atualizado': hoje.strftime('%d/%m/%Y %H:%M')}
    
    except Exception as e:
        return {'error': f'Erro ao acessar sistema: {str(e)}'}

@app.route('/api/producao')
def get_producao():
    """Retorna dados de produção do Banksoft com cache de 1 hora"""
    global _banksoft_cache
    
    forcar = request.args.get('forcar', 'false') == 'true'
    
    with _banksoft_lock:
        agora = datetime.now(BR_TZ)
        cache_valido = (
            _banksoft_cache['data'] is not None and
            _banksoft_cache['updated'] is not None and
            (agora - _banksoft_cache['updated']).seconds < 3600 and
            not forcar
        )
        
        if cache_valido:
            return jsonify({**_banksoft_cache['data'], 'cache': True})
        
        resultado = buscar_producao_banksoft()
        
        if 'error' not in resultado:
            _banksoft_cache = {'data': resultado, 'updated': agora}
        
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
