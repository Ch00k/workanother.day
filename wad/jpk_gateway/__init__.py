"""Filing a JPK_EWP through the Ministry of Finance's document gateway.

The gateway at e-dokumenty.mf.gov.pl takes a JPK in four steps: `InitUploadSigned` opens a
session and hands back an Azure Blob Storage address carrying a one-shot Shared Access
Signature, `Put Blob` puts the document there, `FinishUpload` closes the session and `Status`
answers with the UPO once the document has been processed. What travels is the document
zipped and encrypted with a key generated here and sealed to the Ministry's own certificate.

What makes this reachable at all is the authorisation. A qualified signature would put it out
of reach - this application holds no signing key and should not start - but the Specyfikacja
interfejsów usług JPK 5.5.1 added dane autoryzujące for JPK_EWP(4) on 1 July 2026: a natural
person authorises the file with their NIP, name, date of birth and the revenue figure from
their PIT return for the year two years earlier. The first four are already on the seller,
and the fifth is typed in when the file is sent.
"""
