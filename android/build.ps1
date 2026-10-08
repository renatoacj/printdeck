# Gera o PrintDeck.apk sem Android Studio nem Gradle: só o JDK e o Android SDK (build-tools + platform).
#
#   powershell -ExecutionPolicy Bypass -File android\build.ps1 -Versao 1.0.0 -Codigo 1
#
# Precisa de:
#   -Jdk   pasta de um JDK 17 (padrão: JAVA_HOME ou ~\android-tools\jdk-*)
#   -Sdk   pasta do Android SDK com "platforms;android-34" e "build-tools;34.0.0" (padrão: ANDROID_SDK_ROOT ou ~\android-tools\sdk)
#   -Chave arquivo .jks para assinar. Se não existir, é criado junto com a senha (arquivo ao lado, .senha).
#          GUARDE os dois: sem eles não dá para publicar atualizações que instalem por cima da versão antiga.
param(
    [string]$Versao = "1.0.0",
    [int]$Codigo = 1,
    [string]$Jdk = $(if ($env:JAVA_HOME) { $env:JAVA_HOME } else { (Get-ChildItem "$env:USERPROFILE\android-tools" -Directory -Filter "jdk-*" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName }),
    [string]$Sdk = $(if ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT } else { "$env:USERPROFILE\android-tools\sdk" }),
    [string]$Chave = "$env:USERPROFILE\PrintDeck\android\printdeck-release.jks",
    [string]$Saida = ""
)
$ErrorActionPreference = "Stop"
$aqui = Split-Path -Parent $MyInvocation.MyCommand.Path
$bt = "$Sdk\build-tools\34.0.0"
$plataforma = "$Sdk\platforms\android-34\android.jar"
foreach ($f in "$Jdk\bin\javac.exe", "$bt\aapt2.exe", $plataforma) { if (-not (Test-Path $f)) { throw "Não achei $f (confira -Jdk e -Sdk)" } }
$env:JAVA_HOME = $Jdk
$env:Path = "$Jdk\bin;$env:Path"

function Rodar($exe, $argumentos) {
    & $exe @argumentos
    if ($LASTEXITCODE -ne 0) { throw "Falhou: $(Split-Path -Leaf $exe) $($argumentos -join ' ')" }
}

# pasta de trabalho fora do projeto (o projeto pode estar numa pasta sincronizada)
$obra = Join-Path $env:USERPROFILE "PrintDeck\android\build"
if (Test-Path $obra) { Remove-Item $obra -Recurse -Force }
New-Item -ItemType Directory -Force "$obra\gen", "$obra\classes", "$obra\dex" | Out-Null
# o aapt2 não abre caminhos com acento (ex.: "impressão 3d"): trabalha numa cópia dos fontes
Copy-Item (Join-Path $aqui "res") (Join-Path $obra "res") -Recurse
Copy-Item (Join-Path $aqui "src") (Join-Path $obra "src") -Recurse
Copy-Item (Join-Path $aqui "AndroidManifest.xml") (Join-Path $obra "AndroidManifest.xml")
$aqui = $obra

Write-Host "1/5 recursos"
Rodar "$bt\aapt2.exe" @("compile", "--dir", "$aqui\res", "-o", "$obra\res.zip")
Rodar "$bt\aapt2.exe" @("link", "-o", "$obra\base.apk", "-I", $plataforma, "--manifest", "$aqui\AndroidManifest.xml",
    "--java", "$obra\gen", "--min-sdk-version", "26", "--target-sdk-version", "34",
    "--version-code", "$Codigo", "--version-name", $Versao, "$obra\res.zip")

Write-Host "2/5 codigo"
$fontes = @(Get-ChildItem "$aqui\src", "$obra\gen" -Recurse -Filter *.java | ForEach-Object { $_.FullName })
Rodar "$Jdk\bin\javac.exe" (@("-encoding", "UTF-8", "-source", "11", "-target", "11", "-nowarn", "-Xlint:-options",
    "-classpath", $plataforma, "-d", "$obra\classes") + $fontes)

Write-Host "3/5 dex"
$classes = @(Get-ChildItem "$obra\classes" -Recurse -Filter *.class | ForEach-Object { $_.FullName })
Rodar "$Jdk\bin\java.exe" (@("-cp", "$bt\lib\d8.jar", "com.android.tools.r8.D8", "--release", "--lib", $plataforma,
    "--min-api", "26", "--output", "$obra\dex") + $classes)

Write-Host "4/5 pacote"
Copy-Item "$obra\base.apk" "$obra\sem-assinatura.apk"
Push-Location "$obra\dex"
try { Rodar "$bt\aapt.exe" @("add", "$obra\sem-assinatura.apk", "classes.dex") | Out-Null } finally { Pop-Location }
Rodar "$bt\zipalign.exe" @("-f", "-p", "4", "$obra\sem-assinatura.apk", "$obra\alinhado.apk")

Write-Host "5/5 assinatura"
$senhaArq = "$Chave.senha"
if (-not (Test-Path $Chave)) {
    New-Item -ItemType Directory -Force (Split-Path -Parent $Chave) | Out-Null
    $bytes = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [IO.File]::WriteAllText($senhaArq, [Convert]::ToBase64String($bytes).Replace("+", "a").Replace("/", "b").Replace("=", ""))
    Rodar "$Jdk\bin\keytool.exe" @("-genkeypair", "-keystore", $Chave, "-storepass:file", $senhaArq, "-keypass:file", $senhaArq,
        "-alias", "printdeck", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-dname", "CN=PrintDeck")
    Write-Host "   chave de assinatura criada em $Chave (guarde este arquivo e o .senha!)"
}
if (-not $Saida) { $Saida = Join-Path $env:USERPROFILE "PrintDeck\android\PrintDeck-$Versao.apk" }
Rodar "$Jdk\bin\java.exe" @("-jar", "$bt\lib\apksigner.jar", "sign", "--ks", $Chave, "--ks-key-alias", "printdeck",
    "--ks-pass", "file:$senhaArq", "--out", $Saida, "$obra\alinhado.apk")   # a chave usa a mesma senha do arquivo
Rodar "$Jdk\bin\java.exe" @("-jar", "$bt\lib\apksigner.jar", "verify", "--min-sdk-version", "26", $Saida)
Write-Host ("Pronto: $Saida ({0:N0} KB)" -f ((Get-Item $Saida).Length / 1KB))
