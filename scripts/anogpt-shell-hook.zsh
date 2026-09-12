# Notification événementielle des commandes longues pour ANO-GPT.
# Activation dans ~/.zshrc :
#   source /chemin/vers/ANO-GPT/scripts/anogpt-shell-hook.zsh

autoload -Uz add-zsh-hook
zmodload zsh/datetime 2>/dev/null

typeset -g _ANOGPT_COMMAND=""
typeset -gF _ANOGPT_COMMAND_STARTED=0
typeset -g _ANOGPT_HOOK_DIR="${${(%):-%x}:A:h}"

_anogpt_preexec() {
  _ANOGPT_COMMAND="$1"
  _ANOGPT_COMMAND_STARTED=${EPOCHREALTIME:-$EPOCHSECONDS}
}

_anogpt_precmd() {
  local exit_code=$?
  [[ -z "$_ANOGPT_COMMAND" ]] && return
  local -F duration=$(( ${EPOCHREALTIME:-$EPOCHSECONDS} - _ANOGPT_COMMAND_STARTED ))
  local command_text="$_ANOGPT_COMMAND"
  _ANOGPT_COMMAND=""
  (( duration < 20 )) && return

  local ctl="${_ANOGPT_HOOK_DIR:h}/anogpt-ctl"
  [[ -x "$ctl" ]] || ctl="${commands[anogpt-ctl]:-}"
  [[ -z "$ctl" ]] && return

  local payload
  payload=$(python3 -c 'import json,sys; print(json.dumps({"topic":"terminal","data":{"command":sys.argv[1],"duration_s":float(sys.argv[2]),"exit_code":int(sys.argv[3])}}, ensure_ascii=False))' \
    "$command_text" "$duration" "$exit_code" 2>/dev/null) || return
  command "$ctl" proactive-event "$payload" >/dev/null 2>&1 &!
}

add-zsh-hook preexec _anogpt_preexec
add-zsh-hook precmd _anogpt_precmd
