package com.printdeck.app;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

/**
 * Aplicativo do PrintDeck para Android: mostra o painel do PrintDeck que roda no computador do dono da
 * impressora. Na primeira vez pede o endereço (o "link para os amigos" ou o link fixo do administrador).
 *
 * Só usa o que já vem no Android (sem bibliotecas), para o .apk ficar pequeno e fácil de compilar.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "printdeck";
    private static final String CHAVE_ENDERECO = "endereco";       // ex.: https://printdeck.rede.ts.net
    private static final String ACAO_TROCAR = "com.printdeck.app.TROCAR";
    private static final int PEDIDO_ARQUIVO = 7;

    private static final int FUNDO = Color.parseColor("#111110");
    private static final int CARTAO = Color.parseColor("#1C1C1A");
    private static final int TEXTO = Color.parseColor("#ECEAE4");
    private static final int APAGADO = Color.parseColor("#A8A59C");
    private static final int AZUL = Color.parseColor("#3987E5");
    private static final int BORDA = Color.parseColor("#34332F");

    private FrameLayout raiz;
    private WebView web;
    private ProgressBar barra;
    private View telaExtra;                 // tela de endereço ou de erro, por cima da página
    private ValueCallback<Uri[]> esperandoArquivo;
    private boolean deuErro;

    @Override
    protected void onCreate(Bundle estado) {
        super.onCreate(estado);
        raiz = new FrameLayout(this);
        raiz.setBackgroundColor(FUNDO);
        setContentView(raiz);
        criarWeb();

        if (ACAO_TROCAR.equals(getIntent().getAction()) || endereco() == null) {
            mostrarEndereco(null);
        } else if (estado != null) {
            web.restoreState(estado);
        } else {
            web.loadUrl(endereco() + "/app");
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (ACAO_TROCAR.equals(intent.getAction())) mostrarEndereco(null);
    }

    @Override
    protected void onSaveInstanceState(Bundle estado) {
        super.onSaveInstanceState(estado);
        web.saveState(estado);
    }

    // ------------------------------------------------------------------ página

    private void criarWeb() {
        web = new WebView(this);
        web.setBackgroundColor(FUNDO);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setMediaPlaybackRequiresUserGesture(true);
        s.setUserAgentString(s.getUserAgentString() + " PrintDeckApp/1.0");
        CookieManager.getInstance().setAcceptCookie(true);      // mantém o login entre aberturas do app

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest pedido) {
                Uri uri = pedido.getUrl();
                String base = endereco();
                boolean daqui = base != null && uri.getHost() != null && uri.getHost().equals(Uri.parse(base).getHost());
                if (daqui) return false;                         // páginas do PrintDeck abrem aqui dentro
                abrirFora(uri);                                  // o resto (Tailscale, ntfy...) vai para o navegador
                return true;
            }

            @Override
            public void onPageStarted(WebView v, String url, Bitmap icone) {
                deuErro = false;
                barra.setVisibility(View.VISIBLE);
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                barra.setVisibility(View.GONE);
                CookieManager.getInstance().flush();
                if (!deuErro && telaExtra != null && "erro".equals(telaExtra.getTag())) fecharTelaExtra();
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest pedido, WebResourceError erro) {
                if (pedido.isForMainFrame()) mostrarErro();
            }

            @Override
            public void onReceivedHttpError(WebView v, WebResourceRequest pedido, WebResourceResponse resposta) {
                // 502/504: o link existe, mas o PrintDeck está fechado no computador
                int codigo = resposta.getStatusCode();
                if (pedido.isForMainFrame() && (codigo == 502 || codigo == 503 || codigo == 504)) mostrarErro();
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView v, int progresso) {
                barra.setProgress(progresso);
            }

            // botão "enviar arquivo" da fila: abre o seletor de arquivos do Android (aceita vários .gcode)
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> retorno, FileChooserParams parametros) {
                if (esperandoArquivo != null) esperandoArquivo.onReceiveValue(null);
                esperandoArquivo = retorno;
                Intent escolher = new Intent(Intent.ACTION_GET_CONTENT);
                escolher.addCategory(Intent.CATEGORY_OPENABLE);
                escolher.setType("*/*");
                escolher.putExtra(Intent.EXTRA_ALLOW_MULTIPLE,
                        parametros.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                try {
                    startActivityForResult(Intent.createChooser(escolher, "Escolher o G-code"), PEDIDO_ARQUIVO);
                } catch (ActivityNotFoundException e) {
                    esperandoArquivo = null;
                    return false;
                }
                return true;
            }
        });

        // "Baixar G-code": o gerenciador de downloads do Android baixa com o mesmo login da página
        web.setDownloadListener((url, agente, disposicao, tipo, tamanho) -> {
            try {
                DownloadManager.Request pedido = new DownloadManager.Request(Uri.parse(url));
                String cookies = CookieManager.getInstance().getCookie(url);
                if (cookies != null) pedido.addRequestHeader("Cookie", cookies);
                pedido.addRequestHeader("User-Agent", agente);
                String nome = URLUtil.guessFileName(url, disposicao, tipo);
                pedido.setTitle(nome);
                pedido.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                pedido.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, nome);
                ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(pedido);
                Toast.makeText(this, "Baixando " + nome + "…", Toast.LENGTH_SHORT).show();
            } catch (RuntimeException e) {
                abrirFora(Uri.parse(url));
            }
        });

        raiz.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        barra = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        barra.setMax(100);
        barra.setVisibility(View.GONE);
        raiz.addView(barra, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(3), Gravity.TOP));
    }

    @Override
    protected void onActivityResult(int pedido, int resultado, Intent dados) {
        super.onActivityResult(pedido, resultado, dados);
        if (pedido != PEDIDO_ARQUIVO || esperandoArquivo == null) return;
        Uri[] arquivos = null;
        if (resultado == RESULT_OK && dados != null) {
            if (dados.getClipData() != null) {
                int n = dados.getClipData().getItemCount();
                arquivos = new Uri[n];
                for (int i = 0; i < n; i++) arquivos[i] = dados.getClipData().getItemAt(i).getUri();
            } else if (dados.getData() != null) {
                arquivos = new Uri[]{dados.getData()};
            }
        }
        esperandoArquivo.onReceiveValue(arquivos);
        esperandoArquivo = null;
    }

    @Override
    public void onBackPressed() {
        if (telaExtra != null && "endereco".equals(telaExtra.getTag()) && endereco() != null) {
            fecharTelaExtra();                                   // desistiu de trocar o endereço
        } else if (telaExtra == null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    private void abrirFora(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException ignorado) {
        }
    }

    // ------------------------------------------------------------------ endereço do PrintDeck

    private String endereco() {
        return getSharedPreferences(PREFS, MODE_PRIVATE).getString(CHAVE_ENDERECO, null);
    }

    /**
     * Aceita "printdeck.rede.ts.net", "192.168.1.24:5000" ou o link fixo do administrador inteiro.
     * Guarda só a parte do servidor e abre o que foi digitado (assim o link do administrador já faz o login).
     */
    private boolean conectar(String digitado) {
        String texto = digitado.trim();
        if (texto.isEmpty()) return false;
        if (!texto.matches("(?i)^https?://.*")) {
            boolean local = texto.matches("^(localhost|\\d{1,3}(\\.\\d{1,3}){3})(:\\d+)?(/.*)?$");
            texto = (local ? "http://" : "https://") + texto;
        }
        Uri uri = Uri.parse(texto);
        if (uri.getHost() == null || uri.getHost().isEmpty()) return false;
        String base = uri.getScheme().toLowerCase() + "://" + uri.getHost() + (uri.getPort() > 0 ? ":" + uri.getPort() : "");
        SharedPreferences.Editor e = getSharedPreferences(PREFS, MODE_PRIVATE).edit();
        e.putString(CHAVE_ENDERECO, base).apply();
        String caminho = uri.getPath() == null ? "" : uri.getPath();
        fecharTelaExtra();
        web.clearHistory();
        web.loadUrl(caminho.length() > 1 ? texto : base + "/app");
        return true;
    }

    private void mostrarEndereco(String aviso) {
        LinearLayout caixa = caixaCentral("endereco");
        caixa.addView(texto("PrintDeck", 26, TEXTO, true));
        caixa.addView(espaco(6));
        caixa.addView(texto("Digite o endereço do PrintDeck: o link que o dono da impressora mandou, ou o link fixo do administrador.", 15, APAGADO, false));
        caixa.addView(espaco(18));

        final EditText campo = new EditText(this);
        campo.setHint("printdeck.sua-rede.ts.net");
        campo.setHintTextColor(Color.parseColor("#6F6C64"));
        campo.setTextColor(TEXTO);
        campo.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        campo.setSingleLine(true);
        campo.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        campo.setImeOptions(EditorInfo.IME_ACTION_GO);
        campo.setBackground(fundoArredondado(CARTAO, BORDA));
        campo.setPadding(dp(14), dp(13), dp(14), dp(13));
        if (endereco() != null) campo.setText(endereco());
        caixa.addView(campo, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        final TextView erro = texto(aviso == null ? "" : aviso, 13, Color.parseColor("#F0857A"), false);
        caixa.addView(espaco(8));
        caixa.addView(erro);
        caixa.addView(espaco(10));

        Button entrar = botao("Conectar", true);
        caixa.addView(entrar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(50)));
        View.OnClickListener ir = v -> {
            if (!conectar(campo.getText().toString())) erro.setText("Esse endereço não parece válido. Confira e tente de novo.");
        };
        entrar.setOnClickListener(ir);
        campo.setOnEditorActionListener((v, acao, evento) -> {
            ir.onClick(v);
            return true;
        });

        caixa.addView(espaco(16));
        caixa.addView(texto("Na rede de casa também funciona o endereço local, por exemplo 192.168.1.24:5000.", 13, APAGADO, false));
    }

    private void mostrarErro() {
        deuErro = true;
        barra.setVisibility(View.GONE);
        if (telaExtra != null && "endereco".equals(telaExtra.getTag())) return;
        LinearLayout caixa = caixaCentral("erro");
        caixa.setGravity(Gravity.CENTER_HORIZONTAL);
        TextView emoji = texto("🖨️", 46, TEXTO, false);
        emoji.setGravity(Gravity.CENTER);
        caixa.addView(emoji);
        caixa.addView(espaco(10));
        TextView titulo = texto("O PrintDeck não respondeu", 20, TEXTO, true);
        titulo.setGravity(Gravity.CENTER);
        caixa.addView(titulo);
        caixa.addView(espaco(8));
        TextView explica = texto("O programa precisa estar aberto no computador do dono da impressora. Confira também a sua internet.", 15, APAGADO, false);
        explica.setGravity(Gravity.CENTER);
        caixa.addView(explica);
        caixa.addView(espaco(20));
        Button denovo = botao("Tentar de novo", true);
        denovo.setOnClickListener(v -> {
            deuErro = false;
            String atual = web.getUrl();
            if (atual == null || atual.startsWith("about:") || atual.startsWith("chrome-error:")) web.loadUrl(endereco() + "/app");
            else web.reload();
        });
        caixa.addView(denovo, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(50)));
        caixa.addView(espaco(10));
        Button trocar = botao("Trocar endereço", false);
        trocar.setOnClickListener(v -> mostrarEndereco(null));
        caixa.addView(trocar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(50)));
    }

    // ------------------------------------------------------------------ pecinhas de tela (sem arquivos de layout)

    private LinearLayout caixaCentral(String marca) {
        fecharTelaExtra();
        FrameLayout fundo = new FrameLayout(this);
        fundo.setBackgroundColor(FUNDO);
        fundo.setClickable(true);                                // não deixa o toque passar para a página atrás
        fundo.setTag(marca);
        LinearLayout caixa = new LinearLayout(this);
        caixa.setOrientation(LinearLayout.VERTICAL);
        caixa.setPadding(dp(26), dp(26), dp(26), dp(26));
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER);
        lp.leftMargin = lp.rightMargin = dp(6);
        fundo.addView(caixa, lp);
        raiz.addView(fundo, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        telaExtra = fundo;
        return caixa;
    }

    private void fecharTelaExtra() {
        if (telaExtra != null) {
            raiz.removeView(telaExtra);
            telaExtra = null;
        }
    }

    private TextView texto(String conteudo, int sp, int cor, boolean forte) {
        TextView t = new TextView(this);
        t.setText(conteudo);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(cor);
        t.setLineSpacing(0, 1.25f);
        if (forte) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private Button botao(String rotulo, boolean principal) {
        Button b = new Button(this);
        b.setText(rotulo);
        b.setAllCaps(false);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setTextColor(principal ? Color.WHITE : TEXTO);
        b.setBackground(principal ? fundoArredondado(AZUL, AZUL) : fundoArredondado(CARTAO, BORDA));
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) b.setStateListAnimator(null);
        return b;
    }

    private GradientDrawable fundoArredondado(int cor, int borda) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(cor);
        g.setCornerRadius(dp(11));
        g.setStroke(dp(1), borda);
        return g;
    }

    private View espaco(int altura) {
        View v = new View(this);
        v.setLayoutParams(new LinearLayout.LayoutParams(1, dp(altura)));
        return v;
    }

    private int dp(int valor) {
        return Math.round(valor * getResources().getDisplayMetrics().density);
    }
}
