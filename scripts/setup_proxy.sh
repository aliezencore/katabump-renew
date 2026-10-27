#!/bin/bash
# ============================================================
# 代理启动脚本（仓库内自带版）
#
# 与第三方 setup_proxy.sh 的区别：
#   1. 修复 vmess 在 net=tcp 时生成非法 "transport":{"type":"tcp"} 的问题
#      sing-box 的 transport 只接受 http / ws / quic / grpc / httpupgrade
#   2. 修正 vless/trojan 分支的运算符优先级 bug（[ a ] || [ b ] && x 不是
#      预期的 (a||b)&&x），避免 insecure 被误置为 true
#   3. 启动后做真实连通性验证，失败则明确输出 IS_PROXY=false
#
# 输出：向 $GITHUB_ENV 写入 IS_PROXY / PROXY_SERVER
# ============================================================
export LC_ALL=C
set -e

NODE_LINK="${NODE_LINK:-}"

write_env() {
  if [ -n "$GITHUB_ENV" ]; then
    echo "$1" >> "$GITHUB_ENV"
  else
    export "$1"
  fi
}

if [ -z "$NODE_LINK" ]; then
  echo "[INFO] 未配置 NODE_LINK，直连模式"
  write_env "IS_PROXY=false"
  exit 0
fi

# ---------- 依赖 ----------
if ! command -v jq >/dev/null 2>&1; then
  echo "[INFO] 安装 jq..."
  sudo apt-get update -qq && sudo apt-get install -y -qq jq
fi

if command -v curl >/dev/null 2>&1; then
  DL="curl -fsSL -o"
elif command -v wget >/dev/null 2>&1; then
  DL="wget -qO"
else
  echo "[ERROR] 需要 curl 或 wget"
  exit 1
fi

# ---------- 下载 sing-box ----------
get_latest() {
  curl -s --max-time 20 "https://api.github.com/repos/SagerNet/sing-box/releases" \
    | jq -r '[.[] | select(.prerelease==false)][0].tag_name // empty' \
    | sed 's/^v//'
}

VERSION="$(get_latest || true)"
[ -z "$VERSION" ] && VERSION="1.13.16"
echo "[INFO] sing-box 版本: v${VERSION}"

ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
  x86_64|amd64)   ARCH=amd64 ;;
  x86|i686|i386)  ARCH=386   ;;
  aarch64|arm64)  ARCH=arm64 ;;
  armv7l)         ARCH=armv7 ;;
  s390x)          ARCH=s390x ;;
  *) echo "[ERROR] 不支持的架构: $ARCH_RAW"; exit 1 ;;
esac

download() {
  local v="$1"
  $DL "sing-box.tar.gz" \
    "https://github.com/SagerNet/sing-box/releases/download/v${v}/sing-box-${v}-linux-${ARCH}.tar.gz"
}

if ! download "$VERSION"; then
  echo "[WARN] v${VERSION} 下载失败，回退 v1.13.16"
  VERSION="1.13.16"
  if ! download "$VERSION"; then
    echo "[ERROR] sing-box 下载失败"
    write_env "IS_PROXY=false"
    exit 0
  fi
fi

tar -xzf sing-box.tar.gz
mv "sing-box-${VERSION}-linux-${ARCH}/sing-box" ./
rm -rf sing-box.tar.gz "sing-box-${VERSION}-linux-${ARCH}"
chmod +x sing-box

# ---------- 解析分享链接 ----------
proto="${NODE_LINK%%://*}"
content="${NODE_LINK#*://}"
content="${content%%#*}"

echo "[INFO] 协议: $proto"

url_decode() { printf '%b' "$(echo "$1" | sed 's/%/\\x/g')"; }

# 从 query 中取参数
qget() {
  echo "$1" | tr '&' '\n' | grep -m1 "^$2=" | cut -d= -f2-
}

type=""
server=""
port=""
uuid=""
password=""
flow=""
security="none"
sni=""
fp="chrome"
pbk=""
sid=""
net="tcp"
path="/"
host=""
insecure="false"
obfs_pw=""
alpn=""
congestion="bbr"
username=""
password2=""
version="5"

case "$proto" in
  vmess)
    # vmess://base64(json)
    b64="$content"
    case $(( ${#b64} % 4 )) in
      2) b64="${b64}==" ;;
      3) b64="${b64}=" ;;
    esac
    json="$(echo "$b64" | base64 -d 2>/dev/null || true)"
    if [ -z "$json" ]; then
      echo "[ERROR] vmess base64 解码失败"
      write_env "IS_PROXY=false"; exit 0
    fi
    type="vmess"
    server="$(echo "$json" | jq -r '.add // ""')"
    port="$(echo "$json" | jq -r '.port // 443')"
    uuid="$(echo "$json" | jq -r '.id // ""')"
    net="$(echo "$json" | jq -r '.net // "tcp"')"
    tls_flag="$(echo "$json" | jq -r '.tls // ""')"
    sni="$(echo "$json" | jq -r '.sni // ""')"
    host="$(echo "$json" | jq -r '.host // ""')"
    path="$(echo "$json" | jq -r '.path // "/"')"
    fp="$(echo "$json" | jq -r '.fp // "chrome"')"
    [ "$tls_flag" = "tls" ] && security="tls"
    [ -z "$sni" ] && sni="$server"
    [ -z "$host" ] && host="$server"
    ;;

  vless)
    rest="$content"
    uuid="${rest%%@*}"
    rest="${rest#*@}"
    if [[ "$rest" == *"?"* ]]; then
      hp="${rest%%\?*}"; query="${rest#*\?}"
    else
      hp="$rest"; query=""
    fi
    server="${hp%:*}"; port="${hp##*:}"
    type="vless"
    [ -n "$query" ] && {
      v="$(qget "$query" flow)";        [ -n "$v" ] && flow="$v"
      v="$(qget "$query" type)";        [ -n "$v" ] && net="$v"
      v="$(qget "$query" path)";        [ -n "$v" ] && path="$(url_decode "$v")"
      v="$(qget "$query" host)";        [ -n "$v" ] && host="$v"
      v="$(qget "$query" security)";    [ -n "$v" ] && security="$v"
      v="$(qget "$query" sni)";         [ -n "$v" ] && sni="$v"
      v="$(qget "$query" fp)";          [ -n "$v" ] && fp="$v"
      v="$(qget "$query" pbk)";         [ -n "$v" ] && pbk="$v"
      v="$(qget "$query" sid)";         [ -n "$v" ] && sid="$v"
      v="$(qget "$query" insecure)";    [ "$v" = "1" ] || [ "$v" = "true" ] && insecure="true"
      v="$(qget "$query" allowInsecure)"; [ "$v" = "1" ] || [ "$v" = "true" ] && insecure="true"
    }
    [ -z "$host" ] && host="$server"
    [ -z "$sni" ] && sni="$server"
    ;;

  trojan)
    password="${content%%@*}"
    rest="${content#*@}"
    if [[ "$rest" == *"?"* ]]; then
      hp="${rest%%\?*}"; query="${rest#*\?}"
    else
      hp="$rest"; query=""
    fi
    server="${hp%:*}"; port="${hp##*:}"
    type="trojan"; security="tls"
    [ -n "$query" ] && {
      v="$(qget "$query" type)";  [ -n "$v" ] && net="$v"
      v="$(qget "$query" path)";  [ -n "$v" ] && path="$(url_decode "$v")"
      v="$(qget "$query" host)";  [ -n "$v" ] && host="$v"
      v="$(qget "$query" sni)";   [ -n "$v" ] && sni="$v"
      v="$(qget "$query" fp)";    [ -n "$v" ] && fp="$v"
      v="$(qget "$query" insecure)"; [ "$v" = "1" ] || [ "$v" = "true" ] && insecure="true"
    }
    [ -z "$host" ] && host="$server"
    [ -z "$sni" ] && sni="$server"
    ;;

  hysteria2|hy2)
    if [[ "$content" == *"@"* ]]; then
      password="${content%%@*}"; hp="${content#*@}"
    else
      password=""; hp="$content"
    fi
    if [[ "$hp" == *"?"* ]]; then
      h="${hp%%\?*}"; query="${hp#*\?}"
    else
      h="$hp"; query=""
    fi
    h="${h%/}"
    server="${h%:*}"; port="${h##*:}"
    type="hysteria2"; security="tls"
    [ -n "$query" ] && {
      v="$(qget "$query" obfs)";  [ -n "$v" ] && obfs_pw="$v"
      v="$(qget "$query" sni)";   [ -n "$v" ] && sni="$v"
      v="$(qget "$query" insecure)"; [ "$v" = "1" ] || [ "$v" = "true" ] && insecure="true"
    }
    [ -z "$sni" ] && sni="$server"
    ;;

  tuic)
    up="${content%%@*}"; rest="${content#*@}"
    up_clean="$(echo "$up" | sed 's/%3A/:/g')"
    if [[ "$up_clean" == *":"* ]]; then
      uuid="${up_clean%%:*}"; password2="${up_clean#*:}"
    else
      uuid="$up_clean"; password2=""
    fi
    if [[ "$rest" == *"?"* ]]; then
      hp="${rest%%\?*}"; query="${rest#*\?}"
    else
      hp="$rest"; query=""
    fi
    server="${hp%:*}"; port="${hp##*:}"
    type="tuic"; security="tls"
    [ -n "$query" ] && {
      v="$(qget "$query" sni)"; [ -n "$v" ] && sni="$v"
      v="$(qget "$query" fp)";  [ -n "$v" ] && fp="$v"
      v="$(qget "$query" congestion_control)"; [ -n "$v" ] && congestion="$v"
      v="$(qget "$query" alpn)"; [ -n "$v" ] && alpn="$v"
      v="$(qget "$query" insecure)"; [ "$v" = "1" ] || [ "$v" = "true" ] && insecure="true"
    }
    [ -z "$sni" ] && sni="$server"
    ;;

  anytls)
    password="${content%%@*}"; rest="${content#*@}"
    if [[ "$rest" == *"?"* ]]; then
      hp="${rest%%\?*}"; query="${rest#*\?}"
    else
      hp="$rest"; query=""
    fi
    server="${hp%:*}"; port="${hp##*:}"
    type="anytls"; security="tls"
    [ -n "$query" ] && {
      v="$(qget "$query" sni)"; [ -n "$v" ] && sni="$v"
      v="$(qget "$query" fp)";  [ -n "$v" ] && fp="$v"
      v="$(qget "$query" insecure)"; [ "$v" = "1" ] || [ "$v" = "true" ] && insecure="true"
    }
    [ -z "$sni" ] && sni="$server"
    ;;

  socks5|socks)
    if [[ "$content" == *"@"* ]]; then
      up="${content%%@*}"; hp="${content#*@}"
      dec="$(echo "$up" | base64 -d 2>/dev/null || true)"
      if [ -n "$dec" ] && [[ "$dec" == *":"* ]]; then
        username="${dec%%:*}"; password2="${dec#*:}"
      elif [[ "$up" == *":"* ]]; then
        username="${up%%:*}"; password2="${up#*:}"
      else
        username="$up"; password2=""
      fi
    else
      hp="$content"
    fi
    server="${hp%:*}"; port="${hp##*:}"
    type="socks"
    ;;

  *)
    echo "[ERROR] 不支持的协议: $proto"
    write_env "IS_PROXY=false"; exit 0
    ;;
esac

if [ -z "$server" ] || [ -z "$port" ]; then
  echo "[ERROR] 无法解析服务器地址或端口 (server=$server port=$port)"
  write_env "IS_PROXY=false"; exit 0
fi

echo "[INFO] 节点: $server:$port  transport=$net  tls=$security"

# ---------- 构建 outbound ----------
o="{\"type\":\"$type\",\"tag\":\"proxy\",\"server\":\"$server\",\"server_port\":$port"

# transport：仅当 net 不是 tcp 时才写入（tcp 表示不使用额外传输层）
# sing-box 的 transport 合法类型只有 http / ws / quic / grpc / httpupgrade
case "$net" in
  ""|tcp|none) : ;;
  ws)
    o="$o,\"transport\":{\"type\":\"ws\",\"path\":\"$path\",\"headers\":{\"Host\":\"$host\"}}"
    ;;
  http|h2)
    o="$o,\"transport\":{\"type\":\"http\",\"host\":[\"$host\"],\"path\":\"$path\"}"
    ;;
  grpc)
    o="$o,\"transport\":{\"type\":\"grpc\",\"service_name\":\"$path\"}"
    ;;
  httpupgrade)
    o="$o,\"transport\":{\"type\":\"httpupgrade\",\"host\":\"$host\",\"path\":\"$path\"}"
    ;;
  quic)
    o="$o,\"transport\":{\"type\":\"quic\"}"
    ;;
  *)
    echo "[WARN] 未知 transport '$net'，按 tcp 处理（省略 transport）"
    ;;
esac

case "$type" in
  vmess)
    o="$o,\"uuid\":\"$uuid\",\"security\":\"auto\""
    if [ "$security" = "tls" ]; then
      o="$o,\"tls\":{\"enabled\":true,\"server_name\":\"$sni\",\"insecure\":$insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$fp\"}}"
    fi
    ;;
  vless)
    o="$o,\"uuid\":\"$uuid\""
    [ -n "$flow" ] && o="$o,\"flow\":\"$flow\""
    tls_enabled=false
    if [ "$security" = "tls" ] || [ "$security" = "reality" ]; then
      tls_enabled=true
    fi
    if [ "$tls_enabled" = "true" ]; then
      t="$o,\"tls\":{\"enabled\":true,\"server_name\":\"$sni\",\"insecure\":$insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$fp\"}"
      if [ "$security" = "reality" ]; then
        t="$t,\"reality\":{\"enabled\":true,\"public_key\":\"$pbk\",\"short_id\":\"$sid\"}"
      fi
      o="$t}"
    fi
    ;;
  trojan)
    o="$o,\"password\":\"$password\""
    o="$o,\"tls\":{\"enabled\":true,\"server_name\":\"$sni\",\"insecure\":$insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$fp\"}}"
    ;;
  hysteria2)
    [ -n "$obfs_pw" ] && o="$o,\"obfs\":{\"type\":\"salamander\",\"password\":\"$obfs_pw\"}"
    [ -n "$password" ] && o="$o,\"password\":\"$password\""
    o="$o,\"tls\":{\"enabled\":true,\"server_name\":\"$sni\",\"insecure\":$insecure}"
    ;;
  tuic)
    o="$o,\"uuid\":\"$uuid\""
    [ -n "$password2" ] && o="$o,\"password\":\"$password2\""
    o="$o,\"congestion_control\":\"$congestion\""
    t="{\"enabled\":true,\"server_name\":\"$sni\",\"insecure\":$insecure"
    [ -n "$alpn" ] && t="$t,\"alpn\":[\"$alpn\"]"
    o="$o,\"tls\":$t}"
    ;;
  anytls)
    o="$o,\"password\":\"$password\""
    o="$o,\"tls\":{\"enabled\":true,\"server_name\":\"$sni\",\"insecure\":$insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$fp\"}}"
    ;;
  socks)
    [ -n "$username" ] && o="$o,\"username\":\"$username\""
    [ -n "$password2" ] && o="$o,\"password\":\"$password2\""
    o="$o,\"version\":\"$version\""
    ;;
esac
o="$o}"

cat > sing-box-config.json <<EOF
{
  "log": {"level": "warn"},
  "inbounds": [
    {"type": "socks", "tag": "socks-in", "listen": "127.0.0.1", "listen_port": 1080},
    {"type": "http",  "tag": "http-in",  "listen": "127.0.0.1", "listen_port": 1081}
  ],
  "outbounds": [$o]
}
EOF

if ! jq empty sing-box-config.json 2>/dev/null; then
  echo "[ERROR] 生成的配置不是合法 JSON"
  write_env "IS_PROXY=false"; exit 0
fi

# 用 sing-box 自身校验配置（这一步能提前发现 transport 之类的问题）
if ! ./sing-box check -c sing-box-config.json; then
  echo "[ERROR] sing-box 配置校验失败"
  write_env "IS_PROXY=false"; exit 0
fi
echo "[INFO] 配置校验通过"

# ---------- 启动 ----------
pkill -f sing-box 2>/dev/null || true
sleep 1
./sing-box run -c sing-box-config.json > sing-box.log 2>&1 &
sleep 5

if ! pgrep -f "sing-box run" >/dev/null; then
  echo "[ERROR] sing-box 启动失败:"
  cat sing-box.log
  write_env "IS_PROXY=false"; exit 0
fi

# ---------- 连通性验证 ----------
for i in 1 2 3; do
  if curl -x socks5h://127.0.0.1:1080 -s --max-time 15 https://api.ipify.org >/dev/null 2>&1; then
    ip="$(curl -x socks5h://127.0.0.1:1080 -s --max-time 15 https://api.ipify.org || echo '?')"
    echo "[INFO] 代理连通，出口 IP: $ip"
    write_env "IS_PROXY=true"
    write_env "PROXY_SERVER=socks5://127.0.0.1:1080"
    exit 0
  fi
  echo "[WARN] 连通性测试 $i/3 失败"
  sleep 3
done

echo "[ERROR] 代理无法连通，回退直连"
cat sing-box.log
write_env "IS_PROXY=false"
exit 0
