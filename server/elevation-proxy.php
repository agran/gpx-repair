<?php
/**
 * elevation-proxy.php
 * ---------------------------------------------------------------------------
 * Reverse proxy for the two elevation services used by gpx-repair
 * (https://agran.github.io/gpx-repair/):
 *
 *   - Open-Meteo Elevation API   https://api.open-meteo.com/v1/elevation
 *   - OpenTopoData SRTM30m       https://api.opentopodata.org/v1/srtm30m
 *
 * Only these two are proxied — routing (OSRM) is left untouched, since the
 * connectivity problem is specific to the elevation services.
 *
 * Deploy this file on a server that can reach an outbound HTTP proxy by IP
 * (configured below via OUTBOUND_PROXY). The browser calls THIS script,
 * which in turn fetches the real upstream through the outbound proxy and
 * relays the response back with permissive CORS headers.
 *
 * Usage from the browser:
 *   GET /elevation-proxy.php?url=<url-encoded full upstream URL>
 *
 * Only URLs whose prefix matches $ALLOWED_PREFIXES are forwarded — anything
 * else is rejected with 400, so this cannot be abused as an open proxy.
 * ---------------------------------------------------------------------------
 */

// ─── Configuration ──────────────────────────────────────────────────────────

// Outbound HTTP proxy that upstream requests are routed through.
// Set to '' (empty string) to disable proxying and call upstream directly.
// Can be overridden with the GPX_REPAIR_OUTBOUND_PROXY environment variable.
const OUTBOUND_PROXY = '80.242.56.174:48484';

// Max requests per client IP per minute (a single track repair needs a few
// dozen at most). Excess requests get 429.
const RATE_LIMIT_PER_MIN = 240;

// Origins allowed to call this proxy (CORS). Add more if you serve the app
// from another domain, or '*' to allow everyone (fine for read-only, public,
// rate-limited data like elevation/routing).
const ALLOWED_ORIGINS = [
    'https://agran.github.io',
    'http://localhost',
    'http://127.0.0.1',
    // 'null' — браузер шлёт именно эту строку как Origin, когда страница
    // открыта локально как file:// (например, при тестировании index.html
    // без веб-сервера). Оставлено для удобства локальной разработки.
    'null',
];

// Only these upstream URL prefixes may be requested through this proxy.
const ALLOWED_PREFIXES = [
    'https://api.open-meteo.com/v1/elevation',
    'https://api.opentopodata.org/v1/srtm30m',
];

const REQUEST_TIMEOUT_S = 12;

// ─── CORS ───────────────────────────────────────────────────────────────────

$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
$originAllowed = false;
foreach (ALLOWED_ORIGINS as $allowed) {
    if ($allowed === '*' || $origin === $allowed) {
        $originAllowed = true;
        break;
    }
}
header('Access-Control-Allow-Origin: ' . ($originAllowed ? $origin : 'null'));
header('Vary: Origin');
header('Access-Control-Allow-Methods: GET, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');
header('Access-Control-Max-Age: 86400');

// Preflight — nothing else to do.
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    http_response_code(405);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Only GET is supported']);
    exit;
}

// ─── Validate target URL ────────────────────────────────────────────────────

$target = $_GET['url'] ?? '';
if ($target === '') {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Missing "url" query parameter']);
    exit;
}

$isAllowed = false;
foreach (ALLOWED_PREFIXES as $prefix) {
    if (str_starts_with($target, $prefix)) {
        $isAllowed = true;
        break;
    }
}
if (!$isAllowed) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Target URL is not in the allowlist']);
    exit;
}

// ─── Rate limit (per client IP, fixed one-minute window) ────────────────────

function rateLimited(string $ip): bool
{
    $file = sys_get_temp_dir() . '/gpx-repair-proxy-' . md5($ip) . '.cnt';
    $fh = @fopen($file, 'c+');
    if (!$fh) {
        return false; // fail open: don't break the app if tmp is unwritable
    }
    flock($fh, LOCK_EX);
    $minute = (int) floor(time() / 60);
    $raw = trim((string) stream_get_contents($fh));
    [$m, $count] = array_pad(array_map('intval', explode(':', $raw)), 2, 0);
    $count = ($m === $minute) ? $count + 1 : 1;
    ftruncate($fh, 0);
    rewind($fh);
    fwrite($fh, $minute . ':' . $count);
    flock($fh, LOCK_UN);
    fclose($fh);
    return $count > RATE_LIMIT_PER_MIN;
}

if (rateLimited($_SERVER['REMOTE_ADDR'] ?? '')) {
    http_response_code(429);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Too many requests']);
    exit;
}

// ─── Fetch upstream through the outbound proxy ─────────────────────────────

$ch = curl_init($target);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_FOLLOWLOCATION => false, // the allowlist is checked on the initial URL only
    CURLOPT_PROTOCOLS      => CURLPROTO_HTTPS,
    CURLOPT_TIMEOUT        => REQUEST_TIMEOUT_S,
    CURLOPT_CONNECTTIMEOUT => 5,
    CURLOPT_HTTPHEADER     => [
        'Accept: application/json',
        'User-Agent: gpx-repair-elevation-proxy/1.0',
    ],
]);
$outboundProxy = getenv('GPX_REPAIR_OUTBOUND_PROXY');
if ($outboundProxy === false) {
    $outboundProxy = OUTBOUND_PROXY;
}
if ($outboundProxy !== '') {
    curl_setopt($ch, CURLOPT_PROXY, $outboundProxy);
    curl_setopt($ch, CURLOPT_PROXYTYPE, CURLPROXY_HTTP);
}

$body = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

header('X-Content-Type-Options: nosniff');
header('Content-Type: application/json');

if ($body === false) {
    http_response_code(502);
    echo json_encode(['error' => 'Upstream request failed']);
    exit;
}

http_response_code($httpCode ?: 502);
echo $body;
