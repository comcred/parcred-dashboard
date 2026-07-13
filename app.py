from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials
import os, json, re
from datetime import datetime, timedelta
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
_chromium_ready = False

BANKSOFT_BASE = 'https://parcred.banksofttecnologia.com.br/AppConsig'

TIMEOUT_NAV = 90000       # navegação (login, páginas)
TIMEOUT_DOWNLOAD = 90000  # aguardar o download do CSV começar


def _goto_com_retry(page, url, tentativas=2, timeout=TIMEOUT_NAV):
    """Tenta navegar até `url`, repetindo em caso de timeout antes de desistir."""
    ultimo_erro = None
    for i in range(tentativas):
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            return
        except Exception as e:
            ultimo_erro = e
            page.wait_for_timeout(2000)
    raise ultimo_erro


def buscar_producao_banksoft():
    global _chromium_ready
    debug = {}
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

        # Instala o chromium apenas na primeira vez que este processo roda
        # (evita repetir esse passo caro a cada chamada e reduz risco de timeout)
        if not _chromium_ready:
            import subprocess as sp
            sp.run(['python', '-m', 'playwright', 'install', 'chromium'],
                   capture_output=True, timeout=180)
            _chromium_ready = True

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu',
                      '--disable-setuid-sandbox','--single-process']
            )
            page = browser.new_page()
            page.set_default_timeout(TIMEOUT_NAV)

            # Login (com retry em caso de timeout na navegação)
            _goto_com_retry(page, f'{BASE}/Login/ICLogin', tentativas=2, timeout=TIMEOUT_NAV)
            page.wait_for_timeout(2000)
            page.fill('input[name="txtUsuario$CAMPO"]', usuario)
            page.fill('input[name="txtSenha$CAMPO"]', senha)
            page.click('a:has-text("Acessar"), input[type="submit"], button[type="submit"]')
            page.wait_for_load_state('domcontentloaded', timeout=TIMEOUT_NAV)
            page.wait_for_timeout(3000)

            # Navigate to report
            _goto_com_retry(page, f'{BASE}/Pages/Relatorios/ICRLProducaoAnalitico',
                             tentativas=2, timeout=TIMEOUT_NAV)
            page.wait_for_timeout(3000)

            # Fill dates
            page.fill('input[name="ctl00$Cph$txtFaixaData$edit1$CAMPO"]', dt_ini)
            page.fill('input[name="ctl00$Cph$txtFaixaData$edit2$CAMPO"]', dt_fim)

            # Uncheck all status checkboxes then check only Integrado
            # (capturamos o resultado de cada uma para diagnosticar filtros incompletos)
            debug['checkboxes'] = {}
            for chk in ['chkStatusSimulacao','chkStatusCadastro','chkStatusAndamento',
                        'chkStatusPendente','chkStatusAprovado','chkStatusLiberado','chkStatusReprovado']:
                try:
                    cb = page.locator(f'input[name="ctl00$Cph${chk}"]')
                    estava = cb.is_checked()
                    if estava:
                        cb.uncheck()
                    debug['checkboxes'][chk] = f'ok (estava marcado={estava})'
                except Exception as e:
                    debug['checkboxes'][chk] = f'ERRO: {e}'

            try:
                cb_int = page.locator('input[name="ctl00$Cph$chkStatusIntegrado"]')
                estava = cb_int.is_checked()
                if not estava:
                    cb_int.check()
                debug['checkboxes']['chkStatusIntegrado'] = f'ok (estava marcado={estava})'
            except Exception as e:
                debug['checkboxes']['chkStatusIntegrado'] = f'ERRO: {e}'

            # Tenta detectar e maximizar um seletor de "itens por página" / "quantidade de registros",
            # caso o grid do relatório seja paginado e o CSV exporte só a página atual.
            try:
                pagesize_sel = None
                for sel in page.locator('select').all():
                    try:
                        nome_attr = ((sel.get_attribute('name') or '') + (sel.get_attribute('id') or '')).lower()
                    except Exception:
                        nome_attr = ''
                    if any(h in nome_attr for h in ['pagesize','qtdregistro','qtdpagina','tamanhopagina','registrospagina','itenspagina','ddlqtd']):
                        pagesize_sel = sel
                        break
                if pagesize_sel:
                    opts = [o.strip() for o in pagesize_sel.locator('option').all_text_contents() if o.strip()]
                    todos_opt = next((o for o in opts if 'todos' in o.lower() or 'all' in o.lower()), None)
                    if todos_opt:
                        pagesize_sel.select_option(label=todos_opt)
                        debug['pagesize'] = f'selecionado "{todos_opt}"'
                    else:
                        numericos = [o for o in opts if o.replace('.','').isdigit()]
                        if numericos:
                            maior = max(numericos, key=lambda x: int(x.replace('.','')))
                            pagesize_sel.select_option(label=maior)
                            debug['pagesize'] = f'selecionado "{maior}"'
                    page.wait_for_timeout(1500)
                else:
                    debug['pagesize'] = 'nenhum seletor de itens-por-página encontrado na página'
            except Exception as e:
                debug['pagesize'] = f'ERRO: {e}'

            # Captura qualquer texto de "total de registros" visível na página, para conferência
            try:
                totais = page.locator('text=/[Tt]otal.{0,15}\\d/').all_text_contents()
                debug['totais_na_pagina'] = [t.strip() for t in totais[:6]]
            except Exception as e:
                debug['totais_na_pagina'] = f'ERRO: {e}'

            # Download CSV
            with page.expect_download(timeout=TIMEOUT_DOWNLOAD) as dl:
                page.click('a:has-text("Exportar CSV")')

            download = dl.value
            import tempfile as _tf
            tmp_path = _tf.mktemp(suffix='.csv')
            download.save_as(tmp_path)
            with open(tmp_path, 'rb') as f:
                csv_bytes = f.read()
            try:
                import os as _os
                _os.unlink(tmp_path)
            except: pass
            browser.close()

            # Parse CSV
            import csv, io
            text   = csv_bytes.decode('utf-8-sig', errors='replace')
            reader = csv.DictReader(io.StringIO(text), delimiter=';')
            rows   = [dict(r) for r in reader]

            if not rows or len(rows[0]) < 3:
                return {'error': 'CSV vazio ou inválido', 'preview': text[:200], 'debug': debug}

            return {
                'rows': rows,
                'total': len(rows),
                'periodo': f'{dt_ini} a {dt_fim}',
                'atualizado': hoje.strftime('%d/%m/%Y %H:%M'),
                'debug': debug
            }

    except Exception as e:
        import traceback
        return {'error': str(e), 'traceback': traceback.format_exc()[-800:], 'debug': debug}


import threading as _threading

def _rodar_banksoft_bg():
    with _banksoft_lock:
        resultado = buscar_producao_banksoft()
        if 'error' not in resultado:
            _banksoft_cache['data']    = resultado
            _banksoft_cache['updated'] = datetime.now(BR_TZ)
            _banksoft_cache['running'] = False
        else:
            _banksoft_cache['last_error'] = resultado.get('error','')
            _banksoft_cache['running']    = False

@app.route('/api/producao')
def get_producao():
    forcar = request.args.get('forcar','false') == 'true'
    agora  = datetime.now(BR_TZ)

    # Check cache
    cache_ok = (
        _banksoft_cache.get('data') is not None and
        _banksoft_cache.get('updated') is not None and
        (agora - _banksoft_cache['updated']).seconds < 3600 and
        not forcar
    )
    if cache_ok:
        return jsonify({**_banksoft_cache['data'], 'cache': True})

    # Check if already running
    if _banksoft_cache.get('running'):
        return jsonify({'status': 'processing', 'msg': 'Buscando dados do Banksoft... Tente novamente em 60 segundos.', 'cache': False})

    # Start background task
    _banksoft_cache['running'] = True
    _banksoft_cache['last_error'] = None
    t = _threading.Thread(target=_rodar_banksoft_bg, daemon=True)
    t.start()

    return jsonify({'status': 'processing', 'msg': 'Iniciando busca de dados. Tente novamente em 60 segundos.', 'cache': False})

@app.route('/api/producao/status')
def get_producao_status():
    if _banksoft_cache.get('running'):
        return jsonify({'status': 'processing', 'msg': 'Ainda buscando dados...'})
    if _banksoft_cache.get('data'):
        return jsonify({**_banksoft_cache['data'], 'cache': True, 'status': 'done'})
    erro = _banksoft_cache.get('last_error', 'Nenhuma busca realizada ainda')
    return jsonify({'status': 'idle', 'error': erro})


def get_client():
    creds_dict = json.loads(os.environ.get('GOOGLE_CREDS','{}'))
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet(name):
    return get_client().open_by_key(SHEET_ID).worksheet(name)

PRODUCAO_SHEET_NOME = 'Produção Analitico'

MONEY_COLS = {'Valor Financiado','Valor Líquido','Valor Operação (bruto)','Valor Seguro','Valor TC','Parcela'}
DATE_COLS  = {'Data Digitação','Data Movimentação','Data Nascimento'}


def _serial_para_datetime(serial):
    # Epoch usado pelo Google Sheets / Excel: 30/12/1899
    return datetime(1899, 12, 30) + timedelta(days=float(serial))


def _formatar_celula(header, valor):
    """Normaliza uma célula vinda com UNFORMATTED_VALUE para o formato de texto
    que o dashboard espera, independente de como o Google Sheets exibiria a célula."""
    if valor is None:
        return ''

    if header in DATE_COLS:
        if isinstance(valor, (int, float)):
            dt = _serial_para_datetime(valor)
            if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                return dt.strftime('%d/%m/%Y')
            return dt.strftime('%d/%m/%Y %H:%M:%S')
        s = str(valor).strip()
        m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$', s)
        if m:
            d, mo, y, h, mi, se = m.groups()
            h = h or '00'; mi = mi or '00'; se = se or '00'
            return f'{int(d):02d}/{int(mo):02d}/{y} {int(h):02d}:{int(mi):02d}:{int(se):02d}'
        return s

    if header in MONEY_COLS:
        if isinstance(valor, (int, float)):
            num = float(valor)
        else:
            s = str(valor).strip()
            if not s:
                return ''
            try:
                if ',' in s and '.' in s:
                    s2 = s.replace('.', '').replace(',', '.')
                elif ',' in s:
                    s2 = s.replace(',', '.')
                else:
                    s2 = s
                num = float(s2)
            except ValueError:
                return s
        txt = f'{num:,.2f}'.replace(',', '§').replace('.', ',').replace('§', '.')
        return txt

    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor) if valor is not None else ''


@app.route('/api/producao/sheet/debug')
def get_producao_sheet_debug():
    try:
        ws = get_sheet(PRODUCAO_SHEET_NOME)
        try:
            vals = ws.get_all_values(value_render_option='UNFORMATTED_VALUE')
        except TypeError:
            vals = ws.get_all_values()
        if not vals:
            return jsonify({'error': 'aba vazia'})
        headers = [str(h) for h in vals[0]]
        primeira = vals[1] if len(vals) > 1 else []
        primeira_dict = {headers[i]: primeira[i] for i in range(min(len(headers), len(primeira)))}
        return jsonify({
            'total_linhas': len(vals) - 1,
            'total_colunas': len(headers),
            'headers': headers,
            'primeira_linha_raw': primeira_dict,
        })
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/producao/sheet')
def get_producao_sheet():
    try:
        ws = get_sheet(PRODUCAO_SHEET_NOME)
        try:
            vals = ws.get_all_values(value_render_option='UNFORMATTED_VALUE')
        except TypeError:
            # fallback caso a versão do gspread não aceite esse parâmetro
            vals = ws.get_all_values()

        if not vals or len(vals) < 2:
            return jsonify({'status': 'idle', 'error': f'A aba "{PRODUCAO_SHEET_NOME}" está vazia ou não tem dados.'})

        headers = [str(h).strip() for h in vals[0]]
        rows = []
        for r in vals[1:]:
            if not any(str(c).strip() for c in r):
                continue
            row = {}
            for i, h in enumerate(headers):
                cel = r[i] if i < len(r) else ''
                row[h] = _formatar_celula(h, cel)
            rows.append(row)

        now_br = datetime.now(BR_TZ).strftime('%d/%m/%Y %H:%M')
        return jsonify({
            'status': 'done',
            'rows': rows,
            'total': len(rows),
            'atualizado': now_br,
            'cache': False
        })
    except Exception as e:
        return jsonify({'status': 'idle', 'error': str(e)})

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
