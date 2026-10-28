# Sony Camera Remote SDK: notice

`sony_ctl` (built from `sony_ctl.cpp`, part of Atom Bridge) is the application that controls Sony cameras. It is shipped
here together with the Sony runtime libraries it needs (`libCr_Core.so`, `CrAdapter/`), from the **Sony Camera Remote SDK
v2.02.00**:

- Those library files are © Sony Corporation and/or its licensors. They are licensed under the **Sony Camera Remote SDK
  License Agreement** (http://www.sony.net/CameraRemoteSDK/), **not** under the Atom Bridge license.
- They are distributed only as an inseparable part of the `sony_ctl` application, as the agreement allows (Grant of
  License, (ii)). Do not extract them, use them on their own, or redistribute them apart from Atom Bridge.
- The SDK itself (headers, documentation, samples) is **not** in this repository. To rebuild `sony_ctl`, download the
  SDK from Sony (https://support.d-imaging.sony.co.jp/app/sdk/en/index.html), accept its license, and run
  `compilar.sh <SDK folder or .zip> atom/app/sony`.
- Open source components inside these libraries (libssh2, OpenSSL, libusb): see `OSS-NOTICES.md`.

## What every user must accept before using Atom Bridge

The bridge shows a general notice, valid for every camera and device (not just Sony), once, before the console loads
and on the first visit of the web page (`atom/app/licencas.py`). Nothing works until it is accepted. It includes, for
the Sony libraries:

1. **Warranty.** Once a Sony camera is used or controlled through this application, it may be out of Sony's
   manufacturer warranty, as specified by Sony for that camera. (Users in Australia, New Zealand, the Philippines,
   South Africa and Iraq keep the warranties their consumer law gives them.)
2. **Not Sony.** Atom Bridge is not made, owned, licensed, endorsed or supported by Sony Corporation or its
   affiliates. Support for it comes only from its authors, never from Sony.
3. **Prohibited uses.** The software, the application and the cameras must not be used to design, develop,
   manufacture, use, install, inspect or store arms; to transport explosives, chemical or biological weapons or other
   harmful items; or for military activities such as intelligence, surveillance and reconnaissance.
4. **No warranty, not for high-risk use.** The software is provided "as is", without warranty of any kind, and is not
   fault tolerant: it must not be used where a failure could cause death, injury or severe damage.

Sony is a trademark or registered trademark of Sony Corporation. All other trademarks are the property of their owners.
