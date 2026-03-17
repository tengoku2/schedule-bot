# Python環境にする
FROM python:3.11

# 作業ディレクトリ
WORKDIR /app

# ファイルコピー
COPY . .

# 依存関係インストール
RUN pip install -r requirements.txt

# Bot起動
CMD ["python", "bot.py"]