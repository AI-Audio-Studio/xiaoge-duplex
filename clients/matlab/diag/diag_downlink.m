function diag_downlink(bridgeHost, wavFile, seconds)
%DIAG_DOWNLINK  排查「桥→MATLAB 下行收不到」的诊断脚本(B 方案)。
%   先连下行(5002)再连上行(5001),持续发上行、狂 poll 下行,逐秒打印可用字节。
%
%   用法:
%     diag_downlink('127.0.0.1')                 % 发 2s 静音,收 20s(配 --selftest 桥最直接)
%     diag_downlink('127.0.0.1','speech16k.wav') % 发真实语音(配真机桥)
%     diag_downlink('192.168.x.x','speech16k.wav',25)  % 桥在别的主机
%   speech16k.wav 须 16kHz/单声道/16-bit。
%
%   把整段命令行输出 + 生成的 diag_reply.wav(若有)发回即可。

    if nargin < 1 || isempty(bridgeHost), bridgeHost = '127.0.0.1'; end
    if nargin < 3 || isempty(seconds), seconds = 20; end

    fprintf('==== diag_downlink ====\n');
    fprintf('MATLAB %s | %s\n', version, computer);
    fprintf('bridgeHost=%s  收听时长=%ds\n', bridgeHost, seconds);

    % 准备上行数据
    if nargin >= 2 && ~isempty(wavFile)
        [x, fs] = audioread(wavFile, 'native');
        assert(fs == 16000 && size(x,2) == 1 && isa(x,'int16'), 'wav 须 16kHz/单声道/16-bit');
        fprintf('上行使用 %s(%d 样本)\n', wavFile, numel(x));
    else
        x = zeros(16000*2, 1, 'int16');   % 2s 静音
        fprintf('上行使用 2s 静音(%d 样本)\n', numel(x));
    end

    % 关键:先连下行,再连上行(避免早期音频在桥侧被丢)
    fprintf('连接 down 5002 ...\n');
    down = tcpclient(bridgeHost, 5002, 'Timeout', 2);
    fprintf('  down OK  ByteOrder=%s  NumBytesAvailable=%d\n', down.ByteOrder, down.NumBytesAvailable);
    fprintf('连接 up 5001 ...\n');
    up = tcpclient(bridgeHost, 5001, 'Timeout', 2);
    fprintf('  up OK\n');
    cleanup = onCleanup(@() localClose(up, down)); %#ok<NASGU>

    recv = uint8([]);
    sent = 0; frame = 320; t0 = tic; lastLog = -1; firstRecvT = -1;
    while toc(t0) < seconds
        % 上行:每轮发一帧(20ms)
        if sent < numel(x)
            e = min(sent + frame, numel(x));
            try
                write(up, x(sent+1:e), 'int16');
            catch err
                fprintf('!! 上行 write 出错: %s\n', err.message);
            end
            sent = e;
        end
        % 下行:读走所有可用字节
        n = 0;
        try
            n = down.NumBytesAvailable;
            if n > 0
                b = read(down, n, 'uint8');
                if firstRecvT < 0, firstRecvT = toc(t0); end
                recv = [recv; b(:)]; %#ok<AGROW>
            end
        catch err
            fprintf('!! 下行 read 出错: %s\n', err.message);
        end
        % 逐秒打印
        cur = floor(toc(t0));
        if cur ~= lastLog
            lastLog = cur;
            fprintf('[t=%2ds] up_sent=%dB  down_avail=%dB  down_total=%dB\n', ...
                cur, sent*2, n, numel(recv));
        end
        pause(0.02);
    end

    fprintf('==== 结果 ====\n');
    fprintf('上行共发送 %d 字节\n', sent*2);
    fprintf('下行共收到 %d 字节  首字节到达 t=%.2fs\n', numel(recv), firstRecvT);
    if ~isempty(recv)
        m = floor(numel(recv)/2);
        pcm = typecast(uint8(recv(1:2*m)), 'int16');
        audiowrite('diag_reply.wav', pcm, 16000);
        fprintf('✓ 已存 diag_reply.wav(%d 样本);前 16 字节: %s\n', ...
            numel(pcm), mat2str(recv(1:min(16,numel(recv)))'));
    else
        fprintf(['✗ 下行一个字节都没收到。请对照桥日志判断:\n' ...
                 '   - 桥日志 down_written 是否 > 0?若 >0 而这里为 0 → MATLAB↔桥的 5002 这段有问题\n' ...
                 '     (常见:桥在另一台主机且 5002 被防火墙拦;或 bridgeHost 填错)。\n' ...
                 '   - 桥日志 down_connected 是否为 True?audio_recv 是否 > 0?\n' ...
                 '   - 先用桥的 --selftest 模式单测这一步最快。\n']);
    end
end

function localClose(up, down)
    clear up down
end
