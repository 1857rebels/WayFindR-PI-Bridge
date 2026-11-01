// aa-headless: minimal headless Android Auto head unit built on aasdk.
// Completes the AA session with the phone (through a wireless AA dongle on USB) and
// writes the raw H.264 video stream to a file, or to stdout with "-".
//
// usage: aa-headless [out.h264|-] [margin W or WxH] [dpi] [maps|media|phone|none]

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <thread>
#include <vector>

#include <boost/asio.hpp>
#include <libusb.h>

#include <aasdk/USB/USBHub.hpp>
#include <aasdk/USB/USBWrapper.hpp>
#include <aasdk/USB/AccessoryModeQueryFactory.hpp>
#include <aasdk/USB/AccessoryModeQueryChainFactory.hpp>
#include <aasdk/USB/AOAPDevice.hpp>
#include <aasdk/Transport/USBTransport.hpp>
#include <aasdk/Transport/SSLWrapper.hpp>
#include <aasdk/Messenger/Cryptor.hpp>
#include <aasdk/Messenger/MessageInStream.hpp>
#include <aasdk/Messenger/MessageOutStream.hpp>
#include <aasdk/Messenger/Messenger.hpp>
#include <aasdk/Channel/Control/ControlServiceChannel.hpp>
#include <aasdk/Channel/AV/VideoServiceChannel.hpp>
#include <aasdk/Channel/Sensor/SensorServiceChannel.hpp>
#include <aasdk/Channel/Input/InputServiceChannel.hpp>
#include <aasdk/Channel/AV/MediaAudioServiceChannel.hpp>
#include <aasdk/Channel/AV/SpeechAudioServiceChannel.hpp>
#include <aasdk/Channel/AV/SystemAudioServiceChannel.hpp>
#include <aasdk/Channel/AV/AVInputServiceChannel.hpp>
#include <aasdk/Channel/AV/MediaStatusServiceChannel.hpp>
#include <aasdk_proto/DrivingStatusEnum.pb.h>
#include <aasdk_proto/ButtonCodeEnum.pb.h>

namespace ac = aasdk::channel;
namespace pm = aasdk::proto::messages;
namespace pe = aasdk::proto::enums;

static std::mutex logMutex;
static void log(const std::string& msg)
{
    auto now = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
    std::lock_guard<std::mutex> lock(logMutex);
    std::cerr << std::put_time(std::localtime(&now), "%H:%M:%S ") << msg << std::endl;
}

static std::function<void(aasdk::error::Error)> logError(const std::string& what)
{
    return [what](const aasdk::error::Error& e) { log(what + " failed: " + e.what()); };
}

// Called on any fatal session error; set in main().
static std::function<void()> quitSession;

class VideoHandler: public ac::av::IVideoServiceChannelEventHandler, public std::enable_shared_from_this<VideoHandler>
{
public:
    VideoHandler(boost::asio::io_service& io, aasdk::messenger::IMessenger::Pointer messenger,
                 std::ostream& out, uint32_t marginWidth, uint32_t marginHeight, uint32_t dpi)
        : strand_(io), channel_(std::make_shared<ac::av::VideoServiceChannel>(strand_, std::move(messenger))),
          out_(out), marginWidth_(marginWidth), marginHeight_(marginHeight), dpi_(dpi) {}

    void start() { strand_.dispatch([self = shared_from_this()] { self->channel_->receive(self); }); }

    // Called once, when the first video frame arrives.
    void setOnFirstFrame(std::function<void()> callback) { onFirstFrame_ = std::move(callback); }

    void fill(pm::ServiceDiscoveryResponse& response)
    {
        auto* descriptor = response.add_channels();
        descriptor->set_channel_id(static_cast<uint32_t>(channel_->getId()));
        auto* av = descriptor->mutable_av_channel();
        av->set_stream_type(pe::AVStreamType::VIDEO);
        av->set_available_while_in_call(true);
        auto* config = av->add_video_configs();
        config->set_video_resolution(pe::VideoResolution::_480p);
        config->set_video_fps(pe::VideoFPS::_30);
        config->set_margin_width(marginWidth_);
        config->set_margin_height(marginHeight_);
        config->set_dpi(dpi_);
    }

    void onChannelOpenRequest(const pm::ChannelOpenRequest&) override
    {
        log("video: channel open");
        pm::ChannelOpenResponse response;
        response.set_status(pe::Status::OK);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("video channel open response"));
        channel_->sendChannelOpenResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onAVChannelSetupRequest(const pm::AVChannelSetupRequest& request) override
    {
        log("video: setup request, config " + std::to_string(request.config_index()));
        pm::AVChannelSetupResponse response;
        response.set_media_status(pe::AVChannelSetupStatus::OK);
        response.set_max_unacked(1);
        response.add_configs(0);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([self = shared_from_this()] { self->sendFocus(); }, logError("video setup response"));
        channel_->sendAVChannelSetupResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onAVChannelStartIndication(const pm::AVChannelStartIndication& indication) override
    {
        session_ = indication.session();
        log("video: stream started, session " + std::to_string(session_));
        channel_->receive(shared_from_this());
    }

    void onAVChannelStopIndication(const pm::AVChannelStopIndication&) override
    {
        log("video: stream stopped after " + std::to_string(frames_) + " frames, " + std::to_string(bytes_) + " bytes");
        channel_->receive(shared_from_this());
    }

    void onAVMediaWithTimestampIndication(aasdk::messenger::Timestamp::ValueType, const aasdk::common::DataConstBuffer& buffer) override
    {
        onMedia(buffer);
    }

    void onAVMediaIndication(const aasdk::common::DataConstBuffer& buffer) override { onMedia(buffer); }

    void onVideoFocusRequest(const pm::VideoFocusRequest&) override
    {
        log("video: focus request");
        sendFocus();
        channel_->receive(shared_from_this());
    }

    void onChannelError(const aasdk::error::Error& e) override
    {
        log(std::string("video: channel error: ") + e.what());
        quitSession();
    }

private:
    void sendFocus()
    {
        pm::VideoFocusIndication indication;
        indication.set_focus_mode(pe::VideoFocusMode::FOCUSED);
        indication.set_unrequested(false);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("video focus indication"));
        channel_->sendVideoFocusIndication(indication, std::move(promise));
    }

    void onMedia(const aasdk::common::DataConstBuffer& buffer)
    {
        out_.write(reinterpret_cast<const char*>(buffer.cdata), buffer.size);
        out_.flush();
        bytes_ += buffer.size;
        if (frames_ == 0 && onFirstFrame_)
            onFirstFrame_();
        if (++frames_ == 1 || frames_ % 150 == 0)
            log("video: " + std::to_string(frames_) + " frames, " + std::to_string(bytes_ / 1024) + " KB");

        pm::AVMediaAckIndication ack;
        ack.set_session(session_);
        ack.set_value(1);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("video ack"));
        channel_->sendAVMediaAckIndication(ack, std::move(promise));
        channel_->receive(shared_from_this());
    }

    boost::asio::io_service::strand strand_;
    std::shared_ptr<ac::av::VideoServiceChannel> channel_;
    std::ostream& out_;
    uint32_t marginWidth_;
    uint32_t marginHeight_;
    uint32_t dpi_;
    int32_t session_ = 0;
    uint64_t frames_ = 0;
    uint64_t bytes_ = 0;
    std::function<void()> onFirstFrame_;
};

// The phone expects a driving-status sensor before it will project.
class SensorHandler: public ac::sensor::ISensorServiceChannelEventHandler, public std::enable_shared_from_this<SensorHandler>
{
public:
    SensorHandler(boost::asio::io_service& io, aasdk::messenger::IMessenger::Pointer messenger)
        : strand_(io), channel_(std::make_shared<ac::sensor::SensorServiceChannel>(strand_, std::move(messenger))) {}

    void start() { strand_.dispatch([self = shared_from_this()] { self->channel_->receive(self); }); }

    void fill(pm::ServiceDiscoveryResponse& response)
    {
        auto* descriptor = response.add_channels();
        descriptor->set_channel_id(static_cast<uint32_t>(channel_->getId()));
        auto* sensors = descriptor->mutable_sensor_channel();
        sensors->add_sensors()->set_type(pe::SensorType::DRIVING_STATUS);
        sensors->add_sensors()->set_type(pe::SensorType::NIGHT_DATA);
    }

    void onChannelOpenRequest(const pm::ChannelOpenRequest&) override
    {
        log("sensor: channel open");
        pm::ChannelOpenResponse response;
        response.set_status(pe::Status::OK);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("sensor channel open response"));
        channel_->sendChannelOpenResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onSensorStartRequest(const pm::SensorStartRequestMessage& request) override
    {
        auto type = request.sensor_type();
        log("sensor: start request, type " + std::to_string(type));
        pm::SensorStartResponseMessage response;
        response.set_status(pe::Status::OK);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([self = shared_from_this(), type] { self->sendInitialEvent(type); }, logError("sensor start response"));
        channel_->sendSensorStartResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onChannelError(const aasdk::error::Error& e) override
    {
        log(std::string("sensor: channel error: ") + e.what());
        quitSession();
    }

private:
    void sendInitialEvent(pe::SensorType::Enum type)
    {
        pm::SensorEventIndication indication;
        if (type == pe::SensorType::DRIVING_STATUS)
            indication.add_driving_status()->set_status(pe::DrivingStatus::UNRESTRICTED);
        else if (type == pe::SensorType::NIGHT_DATA)
            indication.add_night_mode()->set_is_night(false);
        else
            return;
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("sensor event"));
        channel_->sendSensorEventIndication(indication, std::move(promise));
    }

    boost::asio::io_service::strand strand_;
    std::shared_ptr<ac::sensor::SensorServiceChannel> channel_;
};

// Now-playing from the phone (title/artist/album and play/pause). Written to the log as
// "media: track <title>\t<artist>\t<album>" and "media: state <play|pause|change|none>" lines,
// which the dash bridge parses and forwards to the dash's own music widget.
class MediaStatusHandler: public ac::av::IMediaStatusServiceChannelEventHandler, public std::enable_shared_from_this<MediaStatusHandler>
{
public:
    MediaStatusHandler(boost::asio::io_service& io, aasdk::messenger::IMessenger::Pointer messenger)
        : strand_(io), channel_(std::make_shared<ac::av::MediaStatusServiceChannel>(strand_, std::move(messenger))) {}

    void start() { strand_.dispatch([self = shared_from_this()] { self->channel_->receive(self); }); }

    void fill(pm::ServiceDiscoveryResponse& response)
    {
        auto* descriptor = response.add_channels();
        descriptor->set_channel_id(static_cast<uint32_t>(channel_->getId()));
        descriptor->mutable_media_infochannel();
    }

    void onChannelOpenRequest(const pm::ChannelOpenRequest&) override
    {
        log("media status: channel open");
        pm::ChannelOpenResponse response;
        response.set_status(pe::Status::OK);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("media status channel open response"));
        channel_->sendChannelOpenResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onMetadataUpdate(const pm::MediaInfoChannelMetadataData& metadata) override
    {
        log("media: track " + clean(metadata.track_name()) + "\t" + clean(metadata.artist_name()) + "\t" +
            clean(metadata.album_name()));
        channel_->receive(shared_from_this());
    }

    void onPlaybackUpdate(const pm::MediaInfoChannelPlaybackData& playback) override
    {
        const char* states[] = {"none", "change", "play", "pause"};
        auto state = static_cast<int>(playback.playback_state());
        log(std::string("media: state ") + (state >= 0 && state < 4 ? states[state] : "none"));
        channel_->receive(shared_from_this());
    }

    // Losing now-playing is no reason to end the projection session.
    void onChannelError(const aasdk::error::Error& e) override
    {
        log(std::string("media status: channel error: ") + e.what());
    }

private:
    static std::string clean(std::string s)
    {
        for (auto& c : s)
            if (c == '\t' || c == '\n' || c == '\r')
                c = ' ';
        return s;
    }

    boost::asio::io_service::strand strand_;
    std::shared_ptr<ac::av::MediaStatusServiceChannel> channel_;
};

// A touchscreen input channel; nothing is sent on it yet, but the phone expects one.
class InputHandler: public ac::input::IInputServiceChannelEventHandler, public std::enable_shared_from_this<InputHandler>
{
public:
    // The touchscreen covers the area AA lays its UI out in: the 800x480 frame minus the margins.
    InputHandler(boost::asio::io_service& io, aasdk::messenger::IMessenger::Pointer messenger,
                 uint32_t marginWidth, uint32_t marginHeight)
        : strand_(io), channel_(std::make_shared<ac::input::InputServiceChannel>(strand_, std::move(messenger))),
          left_(marginWidth / 2), top_(marginHeight / 2),
          width_(800 - marginWidth), height_(480 - marginHeight) {}

    void start() { strand_.dispatch([self = shared_from_this()] { self->channel_->receive(self); }); }

    void fill(pm::ServiceDiscoveryResponse& response)
    {
        auto* descriptor = response.add_channels();
        descriptor->set_channel_id(static_cast<uint32_t>(channel_->getId()));
        auto* input = descriptor->mutable_input_channel();
        auto* touch = input->mutable_touch_screen_config();
        touch->set_width(width_);
        touch->set_height(height_);
        // HOME, BACK, DPAD up/down/left/right/centre, media play/pause/next/previous,
        // AA's own MEDIA / NAVIGATION / TEL buttons (open that app full screen) and the
        // rotary SCROLL_WHEEL
        for (uint32_t code : std::initializer_list<uint32_t>{3, 4, 19, 20, 21, 22, 23, 85, 87, 88,
                              pe::ButtonCode::MEDIA, pe::ButtonCode::NAVIGATION, pe::ButtonCode::TEL,
                              pe::ButtonCode::SCROLL_WHEEL})
            input->add_supported_keycodes(code);
    }

    // Press and release one button, like a steering-wheel key.
    void pressKey(uint32_t code)
    {
        strand_.dispatch([self = shared_from_this(), code] {
            for (bool pressed : {true, false})
            {
                pm::InputEventIndication indication;
                indication.set_timestamp(nowMicros());
                auto* event = indication.mutable_button_event()->add_button_events();
                event->set_scan_code(code);
                event->set_is_pressed(pressed);
                event->set_meta(0);
                event->set_long_press(false);
                auto promise = ac::SendPromise::defer(self->strand_);
                promise->then([] {}, logError("input key event"));
                self->channel_->sendInputEventIndication(indication, std::move(promise));
            }
        });
    }

    struct Pointer { uint32_t x, y, id; };

    // One touch event. Coordinates are in the 800x480 video frame; they are sent relative to the
    // touchscreen (AA's UI area), clamped onto it.
    void touch(pe::TouchAction::Enum action, std::vector<Pointer> pointers, uint32_t actionIndex = 0)
    {
        strand_.dispatch([self = shared_from_this(), action, pointers, actionIndex] {
            pm::InputEventIndication indication;
            indication.set_timestamp(nowMicros());
            auto* event = indication.mutable_touch_event();
            event->set_touch_action(action);
            event->set_action_index(actionIndex);
            for (const auto& p : pointers)
            {
                auto* location = event->add_touch_location();
                location->set_x(std::min(self->width_ - 1, p.x > self->left_ ? p.x - self->left_ : 0));
                location->set_y(std::min(self->height_ - 1, p.y > self->top_ ? p.y - self->top_ : 0));
                location->set_pointer_id(p.id);
            }
            auto promise = ac::SendPromise::defer(self->strand_);
            promise->then([] {}, logError("input touch event"));
            self->channel_->sendInputEventIndication(indication, std::move(promise));
        });
    }

    // Turn the rotary controller by `delta` detents.
    void scroll(int32_t delta)
    {
        strand_.dispatch([self = shared_from_this(), delta] {
            pm::InputEventIndication indication;
            indication.set_timestamp(nowMicros());
            auto* event = indication.mutable_relative_input_event()->add_relative_input_events();
            event->set_scan_code(pe::ButtonCode::SCROLL_WHEEL);
            event->set_delta(delta);
            auto promise = ac::SendPromise::defer(self->strand_);
            promise->then([] {}, logError("input scroll event"));
            self->channel_->sendInputEventIndication(indication, std::move(promise));
        });
    }

private:
    // Wall-clock microseconds, as openauto sends. (steady_clock, i.e. time since the Pi booted,
    // made Maps ignore every touch: only AA's own UI reacted.)
    static uint64_t nowMicros()
    {
        return std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
    }

public:

    void onChannelOpenRequest(const pm::ChannelOpenRequest&) override
    {
        log("input: channel open");
        pm::ChannelOpenResponse response;
        response.set_status(pe::Status::OK);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("input channel open response"));
        channel_->sendChannelOpenResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onBindingRequest(const pm::BindingRequest&) override
    {
        log("input: binding request");
        pm::BindingResponse response;
        response.set_status(pe::Status::OK);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("input binding response"));
        channel_->sendBindingResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onChannelError(const aasdk::error::Error& e) override
    {
        log(std::string("input: channel error: ") + e.what());
        quitSession();
    }

private:
    boost::asio::io_service::strand strand_;
    std::shared_ptr<ac::input::InputServiceChannel> channel_;
    uint32_t left_, top_, width_, height_;
};

// An audio output (media, navigation voice or system sounds). Android Auto refuses to start
// without them. The PCM is acknowledged and dropped for now.
class AudioHandler: public ac::av::IAudioServiceChannelEventHandler, public std::enable_shared_from_this<AudioHandler>
{
public:
    AudioHandler(boost::asio::io_service& io, std::function<std::shared_ptr<ac::av::AudioServiceChannel>(boost::asio::io_service::strand&)> makeChannel,
                 std::string name, pe::AudioType::Enum audioType, uint32_t sampleRate, uint32_t channelCount)
        : strand_(io), channel_(makeChannel(strand_)), name_(std::move(name)), audioType_(audioType),
          sampleRate_(sampleRate), channelCount_(channelCount) {}

    void start() { strand_.dispatch([self = shared_from_this()] { self->channel_->receive(self); }); }

    void fill(pm::ServiceDiscoveryResponse& response)
    {
        auto* descriptor = response.add_channels();
        descriptor->set_channel_id(static_cast<uint32_t>(channel_->getId()));
        auto* av = descriptor->mutable_av_channel();
        av->set_stream_type(pe::AVStreamType::AUDIO);
        av->set_audio_type(audioType_);
        av->set_available_while_in_call(true);
        auto* config = av->add_audio_configs();
        config->set_sample_rate(sampleRate_);
        config->set_bit_depth(16);
        config->set_channel_count(channelCount_);
    }

    void onChannelOpenRequest(const pm::ChannelOpenRequest&) override
    {
        log(name_ + ": channel open");
        pm::ChannelOpenResponse response;
        response.set_status(pe::Status::OK);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError(name_ + " channel open response"));
        channel_->sendChannelOpenResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onAVChannelSetupRequest(const pm::AVChannelSetupRequest&) override
    {
        pm::AVChannelSetupResponse response;
        response.set_media_status(pe::AVChannelSetupStatus::OK);
        response.set_max_unacked(1);
        response.add_configs(0);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError(name_ + " setup response"));
        channel_->sendAVChannelSetupResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onAVChannelStartIndication(const pm::AVChannelStartIndication& indication) override
    {
        session_ = indication.session();
        log(name_ + ": stream started");
        channel_->receive(shared_from_this());
    }

    void onAVChannelStopIndication(const pm::AVChannelStopIndication&) override
    {
        log(name_ + ": stream stopped, " + std::to_string(bytes_ / 1024) + " KB so far");
        channel_->receive(shared_from_this());
    }

    void onAVMediaWithTimestampIndication(aasdk::messenger::Timestamp::ValueType, const aasdk::common::DataConstBuffer& buffer) override
    {
        onMedia(buffer);
    }

    void onAVMediaIndication(const aasdk::common::DataConstBuffer& buffer) override { onMedia(buffer); }

    void onChannelError(const aasdk::error::Error& e) override
    {
        log(name_ + ": channel error: " + e.what());
        quitSession();
    }

private:
    void onMedia(const aasdk::common::DataConstBuffer& buffer)
    {
        bytes_ += buffer.size;
        pm::AVMediaAckIndication ack;
        ack.set_session(session_);
        ack.set_value(1);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError(name_ + " ack"));
        channel_->sendAVMediaAckIndication(ack, std::move(promise));
        channel_->receive(shared_from_this());
    }

    boost::asio::io_service::strand strand_;
    std::shared_ptr<ac::av::AudioServiceChannel> channel_;
    std::string name_;
    pe::AudioType::Enum audioType_;
    uint32_t sampleRate_;
    uint32_t channelCount_;
    int32_t session_ = 0;
    uint64_t bytes_ = 0;
};

template<typename Channel>
static std::shared_ptr<AudioHandler> makeAudio(boost::asio::io_service& io, aasdk::messenger::IMessenger::Pointer messenger,
                                               std::string name, pe::AudioType::Enum type, uint32_t rate, uint32_t channels)
{
    return std::make_shared<AudioHandler>(
        io, [messenger](boost::asio::io_service::strand& strand) { return std::make_shared<Channel>(strand, messenger); },
        std::move(name), type, rate, channels);
}

// Audio channels offered to the phone, from AA_AUDIO (comma list of media, speech, system;
// default all three). A channel not offered stays on the phone, which then plays that audio
// itself, e.g. over Bluetooth to an intercom.
static std::vector<std::shared_ptr<AudioHandler>> makeAudioChannels(boost::asio::io_service& io,
                                                                    aasdk::messenger::IMessenger::Pointer messenger)
{
    const char* env = std::getenv("AA_AUDIO");
    std::string wanted = "," + std::string(env ? env : "media,speech,system") + ",";
    auto has = [&](const char* name) { return wanted.find("," + std::string(name) + ",") != std::string::npos; };
    std::vector<std::shared_ptr<AudioHandler>> audio;
    if (has("media"))
        audio.push_back(makeAudio<ac::av::MediaAudioServiceChannel>(io, messenger, "media audio", pe::AudioType::MEDIA, 48000, 2));
    if (has("speech"))
        audio.push_back(makeAudio<ac::av::SpeechAudioServiceChannel>(io, messenger, "speech audio", pe::AudioType::SPEECH, 16000, 1));
    if (has("system"))
        audio.push_back(makeAudio<ac::av::SystemAudioServiceChannel>(io, messenger, "system audio", pe::AudioType::SYSTEM, 16000, 1));
    log("audio channels offered: " + std::string(env ? env : "media,speech,system"));
    return audio;
}

// Microphone input. Advertised so the phone accepts the head unit; no audio is sent yet.
class MicHandler: public ac::av::IAVInputServiceChannelEventHandler, public std::enable_shared_from_this<MicHandler>
{
public:
    MicHandler(boost::asio::io_service& io, aasdk::messenger::IMessenger::Pointer messenger)
        : strand_(io), channel_(std::make_shared<ac::av::AVInputServiceChannel>(strand_, std::move(messenger))) {}

    void start() { strand_.dispatch([self = shared_from_this()] { self->channel_->receive(self); }); }

    void fill(pm::ServiceDiscoveryResponse& response)
    {
        auto* descriptor = response.add_channels();
        descriptor->set_channel_id(static_cast<uint32_t>(channel_->getId()));
        auto* input = descriptor->mutable_av_input_channel();
        input->set_stream_type(pe::AVStreamType::AUDIO);
        auto* config = input->mutable_audio_config();
        config->set_sample_rate(16000);
        config->set_bit_depth(16);
        config->set_channel_count(1);
    }

    void onChannelOpenRequest(const pm::ChannelOpenRequest&) override
    {
        log("mic: channel open");
        pm::ChannelOpenResponse response;
        response.set_status(pe::Status::OK);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("mic channel open response"));
        channel_->sendChannelOpenResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onAVChannelSetupRequest(const pm::AVChannelSetupRequest&) override
    {
        pm::AVChannelSetupResponse response;
        response.set_media_status(pe::AVChannelSetupStatus::OK);
        response.set_max_unacked(1);
        response.add_configs(0);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("mic setup response"));
        channel_->sendAVChannelSetupResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onAVInputOpenRequest(const pm::AVInputOpenRequest& request) override
    {
        log(std::string("mic: phone ") + (request.open() ? "opened" : "closed") + " the microphone (no audio sent)");
        pm::AVInputOpenResponse response;
        response.set_session(0);
        response.set_value(0);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("mic open response"));
        channel_->sendAVInputOpenResponse(response, std::move(promise));
        channel_->receive(shared_from_this());
    }

    void onAVMediaAckIndication(const pm::AVMediaAckIndication&) override { channel_->receive(shared_from_this()); }

    void onChannelError(const aasdk::error::Error& e) override
    {
        log(std::string("mic: channel error: ") + e.what());
        quitSession();
    }

private:
    boost::asio::io_service::strand strand_;
    std::shared_ptr<ac::av::AVInputServiceChannel> channel_;
};

// Control channel: version check, TLS handshake, service discovery, focus and ping.
class Session: public ac::control::IControlServiceChannelEventHandler, public std::enable_shared_from_this<Session>
{
public:
    Session(boost::asio::io_service& io, aasdk::messenger::ICryptor::Pointer cryptor,
            aasdk::transport::ITransport::Pointer transport, aasdk::messenger::IMessenger::Pointer messenger,
            std::ostream& videoOut, uint32_t marginWidth, uint32_t marginHeight, uint32_t dpi, uint32_t launchKey)
        : strand_(io), cryptor_(std::move(cryptor)), transport_(std::move(transport)), messenger_(messenger),
          control_(std::make_shared<ac::control::ControlServiceChannel>(strand_, messenger)),
          video_(std::make_shared<VideoHandler>(io, messenger, videoOut, marginWidth, marginHeight, dpi)),
          sensor_(std::make_shared<SensorHandler>(io, messenger)),
          input_(std::make_shared<InputHandler>(io, messenger, marginWidth, marginHeight)),
          mic_(std::make_shared<MicHandler>(io, messenger)),
          media_(std::make_shared<MediaStatusHandler>(io, messenger)),
          audio_(makeAudioChannels(io, messenger)),
          launchTimer_(io), launchKey_(launchKey) {}

    void start()
    {
        if (launchKey_ != 0)
        {
            // AA opens its home dashboard first; once video is flowing and AA has settled,
            // press the requested app button (e.g. NAVIGATION) to bring that app up full screen.
            std::weak_ptr<Session> weak = shared_from_this();
            video_->setOnFirstFrame([weak] {
                if (auto self = weak.lock())
                    self->strand_.dispatch([self] {
                        self->launchTimer_.expires_from_now(boost::posix_time::seconds(3));
                        self->launchTimer_.async_wait(self->strand_.wrap([self](const boost::system::error_code& ec) {
                            if (ec)
                                return;
                            log("input: pressing key " + std::to_string(self->launchKey_) + " to open that app");
                            self->input_->pressKey(self->launchKey_);
                        }));
                    });
            });
        }
        strand_.dispatch([self = shared_from_this()] {
            self->cryptor_->init();
            self->video_->start();
            self->sensor_->start();
            self->input_->start();
            self->mic_->start();
            self->media_->start();
            for (auto& audio : self->audio_)
                audio->start();
            self->control_->receive(self);
            auto promise = ac::SendPromise::defer(self->strand_);
            promise->then([] { log("control: version request sent"); }, logError("version request"));
            self->control_->sendVersionRequest(std::move(promise));
        });
    }

    void stop()
    {
        strand_.dispatch([self = shared_from_this()] {
            self->messenger_->stop();
            self->transport_->stop();
            self->cryptor_->deinit();
        });
    }

    void onVersionResponse(uint16_t major, uint16_t minor, pe::VersionResponseStatus::Enum status) override
    {
        log("control: phone protocol " + std::to_string(major) + "." + std::to_string(minor) +
            (status == pe::VersionResponseStatus::MISMATCH ? " (MISMATCH)" : " (match)"));
        if (status == pe::VersionResponseStatus::MISMATCH)
        {
            quitSession();
            return;
        }
        try
        {
            cryptor_->doHandshake();
            sendHandshake();
            control_->receive(shared_from_this());
        }
        catch (const aasdk::error::Error& e)
        {
            onChannelError(e);
        }
    }

    void onHandshake(const aasdk::common::DataConstBuffer& payload) override
    {
        try
        {
            cryptor_->writeHandshakeBuffer(payload);
            if (!cryptor_->doHandshake())
            {
                sendHandshake();
            }
            else
            {
                log("control: TLS handshake complete");
                pm::AuthCompleteIndication indication;
                indication.set_status(pe::Status::OK);
                auto promise = ac::SendPromise::defer(strand_);
                promise->then([] {}, logError("auth complete"));
                control_->sendAuthComplete(indication, std::move(promise));
            }
            control_->receive(shared_from_this());
        }
        catch (const aasdk::error::Error& e)
        {
            onChannelError(e);
        }
    }

    void onServiceDiscoveryRequest(const pm::ServiceDiscoveryRequest& request) override
    {
        log("control: service discovery from " + request.device_brand() + " " + request.device_name());
        pm::ServiceDiscoveryResponse response;
        response.mutable_channels()->Reserve(256);
        response.set_head_unit_name("Wayfindr");
        // The phone remembers settings per car. AA_CAR_ID makes it see a different car (e.g. to
        // re-read the driver side); empty = the original identity.
        const char* carId = std::getenv("AA_CAR_ID");
        const std::string id = carId != nullptr ? carId : "";
        response.set_car_model(id.empty() ? "Mt.Fuji 450" : "Mt.Fuji 450 " + id);
        response.set_car_year("2026");
        response.set_car_serial(id.empty() ? "0" : id);
        // AA lays its UI out for the driver's side; AA_LEFT_HAND_DRIVE=1 asks for the left-hand
        // drive layout (driver on the left).
        const char* lhd = std::getenv("AA_LEFT_HAND_DRIVE");
        response.set_left_hand_drive_vehicle(lhd != nullptr && std::string(lhd) == "1");
        log(std::string("control: left-hand drive ") + (response.left_hand_drive_vehicle() ? "yes" : "no"));
        response.set_headunit_manufacturer("Wayfindr");
        response.set_headunit_model("Pi Zero 2 W");
        response.set_sw_build("1");
        response.set_sw_version("0.1");
        response.set_can_play_native_media_during_vr(false);
        response.set_hide_clock(false);
        video_->fill(response);
        sensor_->fill(response);
        input_->fill(response);
        mic_->fill(response);
        media_->fill(response);
        for (auto& audio : audio_)
            audio->fill(response);

        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] { log("control: service discovery response sent"); }, logError("service discovery response"));
        control_->sendServiceDiscoveryResponse(response, std::move(promise));
        control_->receive(shared_from_this());
    }

    void onAudioFocusRequest(const pm::AudioFocusRequest& request) override
    {
        auto type = request.audio_focus_type();
        pm::AudioFocusResponse response;
        response.set_audio_focus_state(type == pe::AudioFocusType::RELEASE ? pe::AudioFocusState::LOSS
                                       : type == pe::AudioFocusType::GAIN_TRANSIENT ? pe::AudioFocusState::GAIN_TRANSIENT
                                       : pe::AudioFocusState::GAIN);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("audio focus response"));
        control_->sendAudioFocusResponse(response, std::move(promise));
        control_->receive(shared_from_this());
    }

    void onNavigationFocusRequest(const pm::NavigationFocusRequest&) override
    {
        pm::NavigationFocusResponse response;
        response.set_type(2);
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("navigation focus response"));
        control_->sendNavigationFocusResponse(response, std::move(promise));
        control_->receive(shared_from_this());
    }

    void onShutdownRequest(const pm::ShutdownRequest& request) override
    {
        log("control: phone requested shutdown, reason " + std::to_string(request.reason()));
        pm::ShutdownResponse response;
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] { quitSession(); }, [](const aasdk::error::Error&) { quitSession(); });
        control_->sendShutdownResponse(response, std::move(promise));
    }

    void onShutdownResponse(const pm::ShutdownResponse&) override { quitSession(); }

    void onPingRequest(const pm::PingRequest& request) override
    {
        pm::PingResponse response;
        response.set_timestamp(request.timestamp());
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("ping response"));
        control_->sendPingResponse(response, std::move(promise));
        control_->receive(shared_from_this());
    }

    void onPingResponse(const pm::PingResponse&) override { control_->receive(shared_from_this()); }

    void onVoiceSessionRequest(const pm::VoiceSessionRequest&) override { control_->receive(shared_from_this()); }

    std::shared_ptr<InputHandler> input() const { return input_; }

    void onChannelError(const aasdk::error::Error& e) override
    {
        log(std::string("control: channel error: ") + e.what());
        quitSession();
    }

private:
    void sendHandshake()
    {
        auto promise = ac::SendPromise::defer(strand_);
        promise->then([] {}, logError("handshake"));
        control_->sendHandshake(cryptor_->readHandshakeBuffer(), std::move(promise));
    }

    boost::asio::io_service::strand strand_;
    aasdk::messenger::ICryptor::Pointer cryptor_;
    aasdk::transport::ITransport::Pointer transport_;
    aasdk::messenger::IMessenger::Pointer messenger_;
    std::shared_ptr<ac::control::ControlServiceChannel> control_;
    std::shared_ptr<VideoHandler> video_;
    std::shared_ptr<SensorHandler> sensor_;
    std::shared_ptr<InputHandler> input_;
    std::shared_ptr<MicHandler> mic_;
    std::shared_ptr<MediaStatusHandler> media_;
    std::vector<std::shared_ptr<AudioHandler>> audio_;
    boost::asio::deadline_timer launchTimer_;
    uint32_t launchKey_;
};

int main(int argc, char** argv)
{
    std::string outPath = argc > 1 ? argv[1] : "/tmp/aa.h264";
    // Margin: "W" or "WxH" pixels of the 800x480 frame that AA leaves black; it lays its UI
    // out in the centred (800-W)x(480-H) area.
    std::string margin = argc > 2 ? argv[2] : "0";
    uint32_t marginWidth = std::stoul(margin);
    uint32_t marginHeight = margin.find('x') != std::string::npos ? std::stoul(margin.substr(margin.find('x') + 1)) : 0;
    uint32_t dpi = argc > 3 ? std::stoul(argv[3]) : 140;
    // App to open once video starts: "maps" (AA NAVIGATION button), "media", "phone" or "none".
    std::string launch = argc > 4 ? argv[4] : "none";
    uint32_t launchKey = launch == "maps" ? pe::ButtonCode::NAVIGATION
                       : launch == "media" ? pe::ButtonCode::MEDIA
                       : launch == "phone" ? pe::ButtonCode::TEL : 0;

    std::ofstream file;
    if (outPath != "-")
        file.open(outPath, std::ios::binary | std::ios::trunc);
    std::ostream& videoOut = outPath == "-" ? std::cout : file;

    libusb_context* usbContext = nullptr;
    if (libusb_init(&usbContext) != 0)
    {
        log("libusb_init failed");
        return 1;
    }

    boost::asio::io_service io;
    auto work = std::make_unique<boost::asio::io_service::work>(io);
    std::atomic<bool> running{true};

    std::thread usbThread([&] {
        while (running)
        {
            timeval tv{0, 200000};
            libusb_handle_events_timeout_completed(usbContext, &tv, nullptr);
        }
    });
    std::vector<std::thread> ioThreads;
    for (int i = 0; i < 3; ++i)
        ioThreads.emplace_back([&] { io.run(); });

    boost::asio::signal_set signals(io, SIGINT, SIGTERM);
    signals.async_wait([&](const boost::system::error_code&, int) { log("signal, stopping"); io.stop(); });

    aasdk::usb::USBWrapper usbWrapper(usbContext);
    aasdk::usb::AccessoryModeQueryFactory queryFactory(usbWrapper, io);
    aasdk::usb::AccessoryModeQueryChainFactory queryChainFactory(usbWrapper, io, queryFactory);
    auto hub = std::make_shared<aasdk::usb::USBHub>(usbWrapper, io, queryChainFactory);

    std::shared_ptr<Session> session;
    std::mutex sessionMutex;
    std::atomic<bool> quitting{false};
    quitSession = [&] {
        if (quitting.exchange(true))
            return;
        log("session ended");
        if (session)
            session->stop();
        io.post([&] { io.stop(); });
    };

    auto promise = aasdk::usb::IUSBHub::Promise::defer(io);
    promise->then(
        [&](aasdk::usb::DeviceHandle handle) {
            log("usb: device in accessory mode, starting session");
            try
            {
                auto aoapDevice = aasdk::usb::AOAPDevice::create(usbWrapper, io, handle);
                auto transport = std::make_shared<aasdk::transport::USBTransport>(io, aoapDevice);
                auto cryptor = std::make_shared<aasdk::messenger::Cryptor>(std::make_shared<aasdk::transport::SSLWrapper>());
                auto messenger = std::make_shared<aasdk::messenger::Messenger>(
                    io, std::make_shared<aasdk::messenger::MessageInStream>(io, transport, cryptor),
                    std::make_shared<aasdk::messenger::MessageOutStream>(io, transport, cryptor));
                auto s = std::make_shared<Session>(io, cryptor, transport, messenger, videoOut, marginWidth, marginHeight, dpi, launchKey);
                {
                    std::lock_guard<std::mutex> lock(sessionMutex);
                    session = s;
                }
                s->start();
            }
            catch (const aasdk::error::Error& e)
            {
                log(std::string("usb: AOAP device setup failed: ") + e.what());
                quitSession();
            }
        },
        [&](const aasdk::error::Error& e) {
            log(std::string("usb: hub error: ") + e.what());
            io.stop();
        });
    log("waiting for the dongle (output: " + outPath + ", margin " + std::to_string(marginWidth) + "x" + std::to_string(marginHeight) +
        ", dpi " + std::to_string(dpi) + ")");
    hub->start(std::move(promise));

    // Input commands on stdin, one per line, coordinates in the 800x480 frame:
    //   key <code> | scroll <delta> | tap <x> <y> | doubletap <x> <y> | pinch in|out <x> <y>
    //   drag <x1> <y1> <x2> <y2> [ms]
    // (pinch in = fingers spread apart = zoom in). Gestures play out here with real timing.
    std::thread([&] {
        using std::chrono::milliseconds;
        using TA = pe::TouchAction;
        std::string line;
        while (std::getline(std::cin, line))
        {
            std::shared_ptr<InputHandler> input;
            {
                std::lock_guard<std::mutex> lock(sessionMutex);
                if (session)
                    input = session->input();
            }
            std::istringstream args(line);
            std::string cmd;
            args >> cmd;
            if (!input || cmd.empty())
                continue;
            log("input: " + line);
            if (cmd == "key")
            {
                uint32_t code = 0;
                args >> code;
                input->pressKey(code);
            }
            else if (cmd == "scroll")
            {
                int32_t delta = 0;
                args >> delta;
                input->scroll(delta);
            }
            else if (cmd == "tap" || cmd == "doubletap")
            {
                uint32_t x = 400, y = 240;
                args >> x >> y;
                for (int i = 0; i < (cmd == "tap" ? 1 : 2); ++i)
                {
                    if (i)
                        std::this_thread::sleep_for(milliseconds(100));
                    input->touch(TA::PRESS, {{x, y, 0}});
                    std::this_thread::sleep_for(milliseconds(50));
                    input->touch(TA::RELEASE, {{x, y, 0}});
                }
            }
            else if (cmd == "drag")
            {
                uint32_t x1 = 400, y1 = 240, x2 = 500, y2 = 240, ms = 450;
                args >> x1 >> y1 >> x2 >> y2 >> ms;
                const int steps = std::max(5u, ms / 30);
                input->touch(TA::PRESS, {{x1, y1, 0}});
                std::this_thread::sleep_for(milliseconds(80));
                for (int i = 1; i <= steps; ++i)
                {
                    std::this_thread::sleep_for(milliseconds(30));
                    input->touch(TA::DRAG, {{uint32_t(x1 + (int(x2) - int(x1)) * i / steps),
                                             uint32_t(y1 + (int(y2) - int(y1)) * i / steps), 0}});
                }
                std::this_thread::sleep_for(milliseconds(30));
                input->touch(TA::RELEASE, {{x2, y2, 0}});
            }
            else if (cmd == "pinch")
            {
                std::string dir;
                uint32_t x = 400, y = 240;
                args >> dir >> x >> y;
                const int steps = 20;
                double from = dir == "in" ? 25 : 120, to = dir == "in" ? 120 : 25;   // half the finger gap
                auto at = [&](double d) {
                    return std::vector<InputHandler::Pointer>{{uint32_t(x - d), y, 0}, {uint32_t(x + d), y, 1}};
                };
                input->touch(TA::PRESS, {at(from)[0]});
                std::this_thread::sleep_for(milliseconds(20));
                input->touch(TA::POINTER_DOWN, at(from), 1);
                for (int i = 1; i <= steps; ++i)
                {
                    std::this_thread::sleep_for(milliseconds(30));
                    input->touch(TA::DRAG, at(from + (to - from) * i / steps));
                }
                std::this_thread::sleep_for(milliseconds(20));
                input->touch(TA::POINTER_UP, at(to), 1);
                input->touch(TA::RELEASE, {at(to)[0]});
            }
        }
    }).detach();

    for (auto& t : ioThreads)
        t.join();
    running = false;
    usbThread.join();
    videoOut.flush();
    // aasdk objects still hold libusb transfers and io_service handlers here; tearing them
    // down in destructor order crashes (bus error), so leave without running destructors.
    std::_Exit(0);
}
