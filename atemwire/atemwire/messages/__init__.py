# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
ATEM wire-format messages — declarative DSL classes, one file per feature.

Every Send (outgoing) and Recv (incoming) class is declared in a per-feature
module; this package's ``__init__`` re-exports them all so callers can use
either form:

    from atemwire.messages.switching import CutCommand
    from atemwire.messages import CutCommand

The ``RECV_BY_CODE`` mapping is built once at import time by walking the
``Recv`` subclasses; ``atemwire.protocol.AtemProtocol`` uses it to route
incoming packets to the right parser by 4-char wire code.
"""

from atemwire.messages._dsl import Field, Recv, Send

# --- Send classes ----------------------------------------------------------
from atemwire.messages.color_generator import ColorGeneratorCommand
from atemwire.messages.downstream_keyer import (
    DkeyAutoCommand,
    DkeyGainCommand,
    DkeyMaskCommand,
    DkeyOnairCommand,
    DkeyRateCommand,
    DkeySetFillCommand,
    DkeySetKeyCommand,
    DkeyTieCommand,
)
from atemwire.messages.fade_to_black import (
    FadeToBlackCommand,
    FadeToBlackConfigCommand,
    FadeToBlackEnableCommand,
)
from atemwire.messages.fairlight import (
    FairlightCompressorPropertiesCommand,
    FairlightEqBandPropertiesCommand,
    FairlightExpanderPropertiesCommand,
    FairlightLimiterPropertiesCommand,
    FairlightMasterCompressorPropertiesCommand,
    FairlightMasterEqBandPropertiesCommand,
    FairlightMasterLimiterPropertiesCommand,
    FairlightMasterPropertiesCommand,
    FairlightStripPropertiesCommand,
    SendFairlightLevelsCommand,
)
from atemwire.messages.file_transfer import (
    TransferAckCommand,
    TransferDataCommand,
    TransferDownloadRequestCommand,
    TransferFileDataCommand,
    TransferUploadRequestCommand,
)
from atemwire.messages.input_video import (
    InputPropertiesCommand,
    VideoModeCommand,
)
from atemwire.messages.lock import (
    LockCommand,
    PartialLockCommand,
)
from atemwire.messages.macros import (
    MacroActionCommand,
    MacroRecordCommand,
    MacroSleepCommand,
)
from atemwire.messages.media import (
    CaptureStillCommand,
    ClearStillCommand,
    MediaplayerSelectCommand,
)
from atemwire.messages.switching import (
    AutoCommand,
    AuxSourceCommand,
    CutCommand,
    PreviewInputCommand,
    ProgramInputCommand,
)
# (TimeRequestCommand merged into system_info.py — see import block above.)
from atemwire.messages.transition import (
    DipSettingsCommand,
    DveSettingsCommand,
    MixSettingsCommand,
    StingerSettingsCommand,
    TransitionSettingsCommand,
    WipeSettingsCommand,
)
from atemwire.messages.upstream_keyer import (
    KeyCutCommand,
    KeyFillCommand,
    KeyOnAirCommand,
    KeyPropertiesAdvancedChromaColorpickerCommand,
    KeyPropertiesAdvancedChromaCommand,
    KeyPropertiesDveCommand,
    KeyPropertiesLumaCommand,
    KeyPropertiesMaskCommand,
    KeyPropertiesPatternCommand,
    KeyTypeCommand,
    KeyerKeyframeRunCommand,
    KeyerKeyframeSetCommand,
)
# (KeyOnAirCommand merged into upstream_keyer.py — see import block above.)

# --- Recv classes ----------------------------------------------------------
from atemwire.messages.audio_legacy import (
    AtemEqBandPropertiesField,
    AtemMasterEqBandPropertiesField,
    AudioInputField,
    AudioMixerMasterPropertiesField,
    AudioMixerMonitorPropertiesField,
    AudioMixerTallyField,
)
from atemwire.messages.camera_control import CameraControlDataPacketField
from atemwire.messages.color_generator import ColorGeneratorField
# (AutoInputVideoModeField / InitCompleteField / TransferCompleteField
# now live in system_info.py — see the system_info import block below.)
from atemwire.messages.downstream_keyer import (
    DkeyPropertiesBaseField,
    DkeyPropertiesField,
    DkeyStateField,
)
from atemwire.messages.fade_to_black import (
    FadeToBlackEnabledField,
    FadeToBlackField,
    FadeToBlackStateField,
)
from atemwire.messages.fairlight import (
    FairlightAudioInputField,
    FairlightCompressorPropertiesField,
    FairlightExpanderPropertiesField,
    FairlightHeadphonesField,
    FairlightLimiterPropertiesField,
    FairlightMasterCompressorPropertiesField,
    FairlightMasterLimiterPropertiesField,
    FairlightMasterPropertiesField,
    FairlightSoloField,
    FairlightStripDeleteField,
    FairlightStripPropertiesField,
    FairlightTallyField,
)
from atemwire.messages.file_transfer import (
    FileTransferContinueDataField,
    FileTransferDataCompleteField,
    FileTransferDataField,
    FileTransferErrorField,
)
from atemwire.messages.hyperdeck import HyperdeckSettingsField
from atemwire.messages.input_video import (
    InputPropertiesField,
    VideoModeCapabilityField,
    VideoModeField,
)
from atemwire.messages.lock import (
    LockObtainedField,
    LockStateField,
)
from atemwire.messages.macros import (
    MacroPlayStatusField,
    MacroPropertiesField,
    MacroRecordStatusField,
)
from atemwire.messages.manual import ManualField
from atemwire.messages.media import MediaplayerFileInfoField
from atemwire.messages.meters import (
    AudioMeterLevelsField,
    FairlightMasterLevelsField,
    FairlightMeterLevelsField,
)
from atemwire.messages.multiviewer import (
    MultiviewerInputField,
    MultiviewerPropertiesField,
    MultiviewerSafeAreaField,
    MultiviewerVuField,
)
from atemwire.messages.recording import (
    RecordingDiskField,
    RecordingDurationField,
    RecordingSettingsField,
    RecordingStatusField,
)
from atemwire.messages.streaming import (
    StreamingAudioBitrateField,
    StreamingServiceField,
    StreamingStatsField,
    StreamingStatusField,
)
from atemwire.messages.supersource import (
    SupersourceBoxPropertiesField,
    SupersourcePropertiesField,
)
from atemwire.messages.switching import (
    AuxOutputSourceField,
    PreviewBusInputField,
    ProgramBusInputField,
)
from atemwire.messages.system_info import (
    AutoInputVideoModeField,
    FirmwareVersionField,
    InitCompleteField,
    MediaplayerSelectedField,
    MediaplayerSlotsField,
    MixerEffectConfigField,
    ProductNameField,
    Sdi3GLevelField,
    TimeConfigField,
    TimeField,
    TimeRequestCommand,
    TopologyField,
    TransferCompleteField,
)
from atemwire.messages.tally import (
    TallyIndexField,
    TallySourceField,
)
# (TopologyField merged into system_info.py — see import block above.)
from atemwire.messages.transition import (
    TransitionDipField,
    TransitionDveField,
    TransitionMixField,
    TransitionPositionField,
    TransitionPreviewField,
    TransitionSettingsField,
    TransitionStingerField,
    TransitionWipeField,
)
from atemwire.messages.upstream_keyer import (
    KeyOnAirField,
    KeyPropertiesAdvancedChromaColorpickerField,
    KeyPropertiesAdvancedChromaField,
    KeyPropertiesBaseField,
    KeyPropertiesDveField,
    KeyPropertiesFlyField,
    KeyPropertiesFlyKeyframeField,
    KeyPropertiesLumaField,
    KeyPropertiesPatternField,
)


def _build_recv_registries():
    """Walk every Recv subclass at module-import time and build the
    three lookup tables protocol.py needs:

    - ``RECV_BY_CODE``     — 4-char wire code → Recv class
    - ``PRETTY_BY_CODE``   — 4-char wire code → pretty dict-key name
    - ``KEY_FORMAT_BY_PRETTY`` — pretty name → struct.Struct (for indexed
                                  fields like per-M/E or per-strip state)

    ``ManualField`` is excluded — it's a catch-all whose CODE is set
    per-instance, not class-level.
    """
    by_code = {}
    pretty_by_code = {}
    key_format_by_pretty = {}
    seen = set()

    def visit(cls):
        if cls in seen:
            return
        seen.add(cls)
        for sub in cls.__subclasses__():
            visit(sub)
            code = getattr(sub, 'CODE', '')
            if not code or sub is ManualField:
                continue
            by_code[code] = sub
            pretty = getattr(sub, 'PRETTY', '')
            if pretty:
                pretty_by_code[code] = pretty
                kf = getattr(sub, 'KEY_FORMAT', None)
                if kf is not None:
                    key_format_by_pretty[pretty] = kf

    visit(Recv)
    return by_code, pretty_by_code, key_format_by_pretty


RECV_BY_CODE, PRETTY_BY_CODE, KEY_FORMAT_BY_PRETTY = _build_recv_registries()
