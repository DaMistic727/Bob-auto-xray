<?php
declare(strict_types=1);
ini_set('session.use_strict_mode', '1');
ini_set('session.use_only_cookies', '1');
ini_set('display_errors', '0');
session_name('@@COOKIE@@');
session_set_cookie_params(['lifetime'=>0, 'path'=>'/', 'secure'=>true, 'httponly'=>true, 'samesite'=>'Strict']);
session_start();
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
header('Referrer-Policy: no-referrer');
header("Content-Security-Policy: default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
header('X-Request-ID: ' . bin2hex(random_bytes(12)));
$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
$method = $_SERVER['REQUEST_METHOD'];
if ($method === 'POST' && $path === '/@@AUTH@@') {
    $valid = isset($_POST['@@CSRF@@'], $_SESSION['csrf']) && is_string($_POST['@@CSRF@@']) && hash_equals($_SESSION['csrf'], $_POST['@@CSRF@@']);
    // Never store, echo, or log any submitted account fields.
    $_POST = [];
    if (!$valid) { http_response_code(403); echo 'Session expired. Please refresh and try again.'; exit; }
    unset($_SESSION['csrf']);
    session_regenerate_id(true);
    $_SESSION['error'] = true;
    session_write_close();
    usleep(random_int(250000, 650000));
    header('Location: /', true, 303); exit;
}
if ($path !== '/') { http_response_code(404); echo 'Page not found.'; exit; }
if ($method !== 'GET' && $method !== 'HEAD') { header('Allow: GET, HEAD'); http_response_code(405); exit; }
$_SESSION['csrf'] = bin2hex(random_bytes(32));
$csrf = $_SESSION['csrf'];
$error = !empty($_SESSION['error']);
unset($_SESSION['error']);
session_write_close();
if ($method === 'HEAD') { exit; }
?>
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>@@NAME@@ · Private workspace</title><link rel="icon" href="/@@ICON@@.svg" type="image/svg+xml"><link rel="stylesheet" href="/@@STYLE@@.css"></head>
<body><main class="@@LAYOUT@@"><section class="@@ASIDE@@"><a class="brand" href="/"><img src="/@@ICON@@.svg" width="42" height="42" alt="">@@NAME@@</a><div><p class="eyebrow">YOUR PRIVATE WORKSPACE</p><h1>Room for<br>what matters.</h1><p class="intro">A quiet place for your files, ideas, and everything you’re working on.</p><div class="@@ART@@" aria-hidden="true"><span>Documents</span><span>Shared with your team</span><span>All changes saved</span></div></div><small>@@TAGLINE@@</small></section><section class="@@CARD@@"><div class="mobile">@@NAME@@</div><p class="eyebrow">WELCOME BACK</p><h2>Sign in to your space</h2><p class="muted">Continue with your workspace account.</p>
<?php if ($error): ?><p role="alert" class="error">Incorrect username or password.</p><?php endif; ?>
<form method="post" action="/@@AUTH@@" autocomplete="off"><input type="hidden" name="@@CSRF@@" value="<?=htmlspecialchars($csrf, ENT_QUOTES, 'UTF-8')?>"><label for="@@USERID@@">Email or username</label><input id="@@USERID@@" name="@@USERFIELD@@" type="text" required maxlength="254" placeholder="you@workspace.com" autocomplete="off"><label for="@@PASSID@@">Password</label><input id="@@PASSID@@" name="@@PASSFIELD@@" type="password" required maxlength="256" autocomplete="off"><button type="submit">Sign in <span aria-hidden="true">→</span></button></form><p class="help">Need access? Contact your workspace administrator.</p><footer><span class="dot"></span> Private workspace · Secure connection</footer></section></main></body></html>
