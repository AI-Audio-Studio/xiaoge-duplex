classdef XiaogeAudioBlock < matlab.System
    % XiaogeAudioBlock  小歌全双工音频块(Simulink「MATLAB System」块)。
    %   经 xiaoge_bridge.py 的 TCP 桥对接小歌(B 方案,无需 Java)。
    %   每步:输入一帧麦克风 PCM(int16 列向量)→ 发往小歌;输出一帧 TTS PCM。
    %
    %   用法(先在主机起桥):
    %     python clients/matlab/bridge/xiaoge_bridge.py 60.205.197.165 10099 --up 5001 --down 5002 --tls --insecure
    %   再把本块拖入模型,设 BridgeHost/UpPort/DownPort/FrameSize。
    %
    %   音频格式:16000 Hz、单声道、16-bit。FrameSize 默认 320(20ms@16k)。
    %   状态:R2022b 适配,**未在交付环境运行**,请按 README 验证。

    properties (Nontunable)
        BridgeHost = '127.0.0.1'   % 桥主机
        UpPort = 5001              % 上行 TCP 端口(麦克风→小歌)
        DownPort = 5002            % 下行 TCP 端口(小歌 TTS→此)
        FrameSize = 320            % 每步样本数
    end

    properties (Access = private)
        UpClient
        DownClient
    end

    methods (Access = protected)
        function setupImpl(obj)
            obj.UpClient = tcpclient(obj.BridgeHost, obj.UpPort, 'Timeout', 1);
            obj.DownClient = tcpclient(obj.BridgeHost, obj.DownPort, 'Timeout', 1);
        end

        function y = stepImpl(obj, u)
            write(obj.UpClient, int16(u(:)), 'int16');     % 上行
            n = obj.FrameSize;
            y = zeros(n, 1, 'int16');
            avail = floor(obj.DownClient.NumBytesAvailable / 2);
            k = min(avail, n);
            if k > 0
                y(1:k) = read(obj.DownClient, k, 'int16');  % 下行(不足补静音)
            end
        end

        function releaseImpl(obj)
            obj.UpClient = [];
            obj.DownClient = [];
        end

        function out = getOutputSizeImpl(obj)
            out = [obj.FrameSize 1];
        end

        function out = getOutputDataTypeImpl(~)
            out = 'int16';
        end

        function c = isOutputComplexImpl(~)
            c = false;
        end

        function s = isOutputFixedSizeImpl(~)
            s = true;
        end
    end
end
