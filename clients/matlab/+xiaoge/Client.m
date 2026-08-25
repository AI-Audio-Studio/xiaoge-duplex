classdef Client < handle
    %xiaoge.Client R5.2.2 MATLAB TCP client for xiaoge_bridge.py.
    %   The Python bridge owns create_session, WSS Bearer auth, ctrl.hello,
    %   cmd_ack/cmd_result, and fake executor behavior. MATLAB sends PCM to the
    %   bridge and receives TTS PCM plus JSONL events.

    properties
        OnAudio = []
        OnEvent = []
    end

    properties (Access = private)
        UpClient
        DownClient
        EventsClient
    end

    methods
        function obj = Client(bridgeHost, upPort, downPort, eventsPort)
            if nargin < 1, bridgeHost = '127.0.0.1'; end
            if nargin < 2, upPort = 5001; end
            if nargin < 3, downPort = 5002; end
            if nargin < 4, eventsPort = 5003; end
            obj.UpClient = tcpclient(bridgeHost, upPort, 'Timeout', 1);
            obj.DownClient = tcpclient(bridgeHost, downPort, 'Timeout', 1);
            obj.EventsClient = tcpclient(bridgeHost, eventsPort, 'Timeout', 1);
        end

        function sendPcm(obj, pcm)
            write(obj.UpClient, int16(pcm(:)), 'int16');
        end

        function pcm = readAudio(obj, maxSamples)
            if nargin < 2, maxSamples = 320; end
            k = min(floor(obj.DownClient.NumBytesAvailable / 2), maxSamples);
            pcm = int16([]);
            if k > 0
                pcm = read(obj.DownClient, k, 'int16');
                if ~isempty(obj.OnAudio), obj.OnAudio(pcm); end
            end
        end

        function events = readEvents(obj)
            events = {};
            n = obj.EventsClient.NumBytesAvailable;
            if n <= 0, return; end
            raw = char(read(obj.EventsClient, n, 'uint8')');
            lines = regexp(raw, '\r?\n', 'split');
            for i = 1:numel(lines)
                if strlength(lines{i}) == 0, continue; end
                ev = jsondecode(lines{i});
                events{end + 1} = ev; %#ok<AGROW>
                if ~isempty(obj.OnEvent), obj.OnEvent(ev); end
            end
        end

        function close(obj)
            obj.UpClient = [];
            obj.DownClient = [];
            obj.EventsClient = [];
        end
    end
end
