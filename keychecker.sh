#!/bin/bash
# ==============================
# Gemini API Key Tester v1.0
# Tests multiple keys against all available models
# Usage: ./gemini_key_tester.sh keys.txt
#        ./gemini_key_tester.sh keys.txt --summary
#        ./gemini_key_tester.sh keys.txt --fast
# ==============================

# ==============================
# 1. Input validation
# ==============================
KEYS_FILE="${1:-keys.txt}"
MODE="${2:-full}"

if [ ! -f "$KEYS_FILE" ]; then
    echo "Usage: $0 <keys_file> [--summary|--fast]"
    echo ""
    echo "  keys_file    Text file with one API key per line (# for comments)"
    echo "  --summary    Only show pass/fail count per key (skip model details)"
    echo "  --fast       Test only 3 core models per key (quick health check)"
    echo ""
    echo "Example:"
    echo "  echo 'AIzaSy...' > keys.txt"
    echo "  echo 'AIzaSy...' >> keys.txt"
    echo "  $0 keys.txt"
    exit 1
fi

# Read keys, skip blanks and comments
KEYS=()
while IFS= read -r line; do
    stripped=$(echo "$line" | sed 's/#.*//' | xargs)
    [ -n "$stripped" ] && KEYS+=("$stripped")
done < "$KEYS_FILE"

if [ ${#KEYS[@]} -eq 0 ]; then
    echo "No keys found in $KEYS_FILE"
    exit 1
fi

# ==============================
# 2. Model list
# ==============================
ALL_MODELS=(
    "gemini-1.5-flash"
    "gemini-1.5-flash-8b"
    "gemini-1.5-pro"
    "gemini-2.0-flash"
    "gemini-2.0-flash-lite"
    "gemini-2.5-flash"
    "gemini-2.5-flash-lite"
    "gemini-2.5-flash-lite-preview-06-17"
    "gemini-2.5-flash-lite-preview-09"
    "gemini-2.5-flash-preview-04-17"
    "gemini-2.5-flash-preview-05-20"
    "gemini-2.5-flash-preview-09-2025"
    "gemini-2.5-pro"
    "gemini-2.5-pro-preview-05-06"
    "gemini-2.5-pro-preview-06-05"
    "gemini-3-flash-preview"
    "gemini-3-pro-preview"
    "gemini-3.1-pro-preview"
    "gemini-3.1-pro-preview-customtools"
    "gemini-flash-latest"
    "gemini-flash-lite-latest"
    "gemini-live-2.5-flash"
    "gemini-live-2.5-flash-preview-native"
)

FAST_MODELS=(
    "gemini-2.0-flash"
    "gemini-2.5-flash"
    "gemini-3-flash-preview"
)

if [ "$MODE" = "--fast" ]; then
    MODELS=("${FAST_MODELS[@]}")
else
    MODELS=("${ALL_MODELS[@]}")
fi

# ==============================
# 3. Counters
# ==============================
TOTAL_KEYS=${#KEYS[@]}
TOTAL_MODELS=${#MODELS[@]}
REPORT_FILE="key_test_report_$(date +%Y%m%d_%H%M%S).txt"

echo "=========================================="
echo " Gemini API Key Tester"
echo " Keys:   $TOTAL_KEYS (from $KEYS_FILE)"
echo " Models: $TOTAL_MODELS"
echo " Mode:   ${MODE/--/}"
echo " Report: $REPORT_FILE"
echo "=========================================="
echo ""

# Also write to report file
exec > >(tee -a "$REPORT_FILE") 2>&1

# ==============================
# 4. Test loop
# ==============================
KEY_NUM=0
SUMMARY=()

for API_KEY in "${KEYS[@]}"; do
    KEY_NUM=$((KEY_NUM + 1))
    KEY_SHORT="...${API_KEY: -8}"
    PASS=0
    FAIL=0
    QUOTA=0
    NOTFOUND=0
    DISABLED=0
    ERRORS=0

    echo "------------------------------------------"
    echo " Key $KEY_NUM/$TOTAL_KEYS: $KEY_SHORT"
    echo "------------------------------------------"

    # Quick validation - check prefix
    if [[ ! "$API_KEY" =~ ^AIzaSy ]]; then
        echo " [WARN] Key doesn't start with AIzaSy - might not be a Google key"
    fi

    for MODEL in "${MODELS[@]}"; do
        if [ "$MODE" != "--summary" ]; then
            echo -n "  [Test] ${MODEL}: "
        fi

        RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
            "https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=${API_KEY}" \
            -H 'Content-Type: application/json' \
            -d '{
                "contents": [{"parts":[{"text": "ping"}]}],
                "generationConfig": {"maxOutputTokens": 5}
            }' \
            --connect-timeout 10 \
            --max-time 30)

        # Split response body and HTTP status
        HTTP_CODE=$(echo "$RESPONSE" | tail -1)
        BODY=$(echo "$RESPONSE" | sed '$d')

        if echo "$BODY" | grep -q '"text"'; then
            PASS=$((PASS + 1))
            [ "$MODE" != "--summary" ] && echo -e "\e[32m[OK]\e[0m"
        elif echo "$BODY" | grep -q "RESOURCE_EXHAUSTED"; then
            QUOTA=$((QUOTA + 1))
            [ "$MODE" != "--summary" ] && echo -e "\e[31m[429 QUOTA]\e[0m"
        elif echo "$BODY" | grep -q "MODEL_NOT_FOUND" || echo "$BODY" | grep -q "404"; then
            NOTFOUND=$((NOTFOUND + 1))
            [ "$MODE" != "--summary" ] && echo -e "\e[33m[404 NOT FOUND]\e[0m"
        elif echo "$BODY" | grep -q "PERMISSION_DENIED"; then
            DISABLED=$((DISABLED + 1))
            [ "$MODE" != "--summary" ] && echo -e "\e[35m[DENIED]\e[0m"
        elif echo "$BODY" | grep -q "API_KEY_INVALID"; then
            FAIL=$((FAIL + 1))
            [ "$MODE" != "--summary" ] && echo -e "\e[31m[INVALID KEY]\e[0m"
            # No point testing more models with a dead key
            echo "  Key is invalid - skipping remaining models"
            break
        else
            ERRORS=$((ERRORS + 1))
            ERROR_MSG=$(echo "$BODY" | grep "message" | head -1 | cut -d'"' -f4)
            [ "$MODE" != "--summary" ] && echo -e "\e[36m[ERR: ${ERROR_MSG:-HTTP $HTTP_CODE}]\e[0m"
        fi

        # Small delay to avoid hammering
        sleep 0.2
    done

    # Key summary
    echo ""
    echo "  Results for $KEY_SHORT:"
    echo "    OK: $PASS  |  Quota: $QUOTA  |  404: $NOTFOUND  |  Denied: $DISABLED  |  Error: $ERRORS"

    # Health rating
    if [ $PASS -ge $((TOTAL_MODELS / 2)) ]; then
        HEALTH="GOOD"
        echo -e "    Health: \e[32m$HEALTH\e[0m"
    elif [ $PASS -gt 0 ]; then
        HEALTH="PARTIAL"
        echo -e "    Health: \e[33m$HEALTH\e[0m"
    elif [ $QUOTA -gt 0 ]; then
        HEALTH="EXHAUSTED"
        echo -e "    Health: \e[31m$HEALTH\e[0m"
    else
        HEALTH="DEAD"
        echo -e "    Health: \e[31m$HEALTH\e[0m"
    fi

    SUMMARY+=("Key $KEY_NUM ($KEY_SHORT): $HEALTH - OK:$PASS Quota:$QUOTA 404:$NOTFOUND Denied:$DISABLED Err:$ERRORS")
    echo ""
done

# ==============================
# 5. Final summary
# ==============================
echo "=========================================="
echo " SUMMARY"
echo "=========================================="
for S in "${SUMMARY[@]}"; do
    echo "  $S"
done
echo ""

# Count healthy keys
GOOD_KEYS=0
for S in "${SUMMARY[@]}"; do
    if echo "$S" | grep -q "GOOD\|PARTIAL"; then
        GOOD_KEYS=$((GOOD_KEYS + 1))
    fi
done

echo "  Usable keys: $GOOD_KEYS / $TOTAL_KEYS"
echo "  Report saved: $REPORT_FILE"
echo "=========================================="
