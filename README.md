# zuxia-Checkin
足下成长系统-每日打卡、风采展示自动打卡脚本
## 环境
python  
requests  
```
pip install requests
```
## 使用
下载[punch.py](punch.py)  
修改账号密码，图片路径的配置  
cron
```
0 8 * * * cd /路径/zuxia && /路径/zuxia/.venv/bin/python punch.py >> /路径/zuxia/logs/punch.log 2>&1
```
