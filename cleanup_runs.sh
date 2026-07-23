#!/bin/bash
# 删除 runs/ 下 3 天前的会话目录
RUNS_DIR="/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main/runs"
find "$RUNS_DIR" -maxdepth 1 -mindepth 1 -type d -mtime +3 -exec rm -rf {} +
