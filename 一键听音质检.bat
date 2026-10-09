@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
title 听音质检 - 一键流水线

echo ============================================
echo   听音质检 一键流水线
echo   抓取录音 -^> 语音转文字 -^> AI质检
echo ============================================
echo.
echo 参数（可选）：--start 2026-09-01 --end 2026-09-30 --call-type all
echo 不传参数则处理「今天 / 外呼+接电 / 接通」。
echo.

echo [1/2] 抓取 + 转写 + 质检 ...
echo.
python run_pipeline.py --mode full %*
if %errorlevel% neq 0 (
    echo.
    echo [警告] 流水线执行出错，请查看上方日志。
)
echo.
echo --------------------------------
echo.

echo [2/2] 补跑尚未完成的质检 ...
echo.
python run_pipeline.py --mode qc %*

echo.
echo ============================================
echo   完成！结果文件：output\听音质检结果.xlsx
echo ============================================
echo.
pause
