# Shared by start.sh and stop.sh.
project_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$project_root"
service_name=${project_root##*/}
state_dir="$project_root/run-scripts/.runtime"
pid_file="$state_dir/service.pid"
log_file="$state_dir/service.log"

if ! command -v lsof >/dev/null; then
    echo "启停脚本需要 lsof，请先安装。" >&2
    exit 1
fi
mkdir -p -- "$state_dir"

# Serialize start and stop. The service closes fd 9 when it launches.
exec 9>>"$state_dir/control.lock"
if command -v flock >/dev/null; then
    flock 9
else
    lockf 9
fi

managed_pid() {
    [[ -f "$pid_file" ]] && IFS= read -r pid < "$pid_file" &&
        [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != 1 ]] &&
        lsof -nP -a -p "$pid" -d 8 -t -- "$pid_file" >/dev/null 2>&1
}

prepare_start() {
    if managed_pid; then
        echo "$service_name 已在运行（PID ${pid}）。"
        exit 0
    fi
    if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
        echo "PORT 必须是 1 到 65535 之间的整数。" >&2
        exit 1
    fi
}

stop_service() {
    if managed_pid; then
        if ! kill -TERM "$pid" 2>/dev/null && managed_pid; then
            echo "$service_name 停止失败（PID ${pid}）。" >&2
            return 1
        fi
        local deadline=$((SECONDS + 15))
        while managed_pid; do
            if (( SECONDS >= deadline )); then
                echo "$service_name 仍在退出（PID ${pid}），请查看 ${log_file}。" >&2
                return 1
            fi
            sleep 0.2
        done
        echo "$service_name 已停止。"
    else
        echo "$service_name 未运行。"
    fi
    if [[ -e "$pid_file" ]]; then
        rm -- "$pid_file"
    fi
}

launch_service() {
    # Keep the PID file open on fd 8 so stop can verify even a recycled PID.
    nohup "$@" </dev/null >"$log_file" 2>&1 8>>"$pid_file" 9>&- &
    pid=$!
    printf '%s\n' "$pid" >"$pid_file"

    local deadline=$((SECONDS + 30))
    while kill -0 "$pid" 2>/dev/null; do
        if lsof -nP -a -p "$pid" -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
            printf '%s 已启动（PID %s，端口 %s）。\n日志 %s\n' "$service_name" "$pid" "$port" "$log_file"
            return 0
        fi
        if (( SECONDS >= deadline )); then
            echo "$service_name 等待端口 $port 超时，请查看 ${log_file}。" >&2
            stop_service
            return 1
        fi
        sleep 0.2
    done
    rm -- "$pid_file"
    echo "$service_name 启动失败，请查看 ${log_file}。" >&2
    tail -n 15 "$log_file" >&2
    return 1
}
