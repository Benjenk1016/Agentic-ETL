#!/bin/bash
# Automatically pull the required Ollama model on first startup

MODEL_NAME="qwen3:4b"

echo "Checking if Ollama model ${MODEL_NAME} is available..."

# Wait for Ollama service to be ready
until ollama list &> /dev/null; do
    echo "Waiting for Ollama service to start..."
    sleep 2
done

# Check if model exists
if ! ollama list | grep -q "${MODEL_NAME}"; then
    echo "Model ${MODEL_NAME} not found. Pulling..."
    ollama pull "${MODEL_NAME}"
    echo "Model ${MODEL_NAME} pulled successfully!"
else
    echo "Model ${MODEL_NAME} already available."
fi
