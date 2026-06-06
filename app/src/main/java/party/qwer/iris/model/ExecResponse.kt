package party.qwer.iris.model

import kotlinx.serialization.Serializable

@Serializable
data class ExecResponse(val stdout: String, val exitCode: Int)
