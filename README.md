# Dashboard Comercial Parcred

## Deploy no Render

1. Faça upload desta pasta para um repositório GitHub
2. No Render, crie um novo Web Service apontando para esse repositório
3. Configure a variável de ambiente GOOGLE_CREDS (veja abaixo)

## Como gerar as credenciais do Google

1. Acesse console.cloud.google.com
2. Crie um projeto ou use um existente
3. Ative a API "Google Sheets API"
4. Vá em "Credenciais" → "Criar credenciais" → "Conta de serviço"
5. Baixe o JSON da conta de serviço
6. Copie TODO o conteúdo do JSON e cole na variável GOOGLE_CREDS do Render
7. Compartilhe a planilha com o e-mail da conta de serviço (como Editor)
