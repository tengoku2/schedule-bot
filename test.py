import mysql.connector
import os

db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="パスワード",
    database="taskbot"
)

print("接続成功！")